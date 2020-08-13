# coding=utf-8
# Copyright 2020 The Cloudminds Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This file implements the functionalities of a minitaur using pybullet."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import copy
import math
import re
import typing
import numpy as np
from robots import robot_motor
from robots import action_filter
from sim import pybullet_env
from utils import transformations


OVERHEAT_SHUTDOWN_TORQUE = 2.45
OVERHEAT_SHUTDOWN_TIME = 1.0
MAX_MOTOR_ANGLE_CHANGE_PER_STEP = 0.2
SENSOR_NOISE_STDDEV = (0.0, 0.0, 0.0, 0.0, 0.0)
TWO_PI = 2 * math.pi


class Quadruped(pybullet_env.PybulletEnv):
    """The minitaur class that simulates a quadruped robot from Ghost Robotics."""

    def __init__(self, robot):
        """Constructs a minitaur and reset it to the initial states.
        """
        if robot == "laikago":
            from robots import laikago as robot
        elif robot == "mini_cheetah":
            from robots import mini_cheetah as robot
        else:
            raise ValueError("wrong robot select")

        self.action_space = robot.action_space
        self.observation_space = robot.observation_space
        self._urdf_file = robot.URDF_FILENAME
        self.num_motors = robot.NUM_MOTORS
        self.name_motor = robot.MOTOR_NAMES
        self.pattern = robot.PATTERN
        self.num_legs = self.num_motors // robot.DOFS_PER_LEG
        self.sim_time_step = robot.T_STEP
        self._action_repeat = robot.NUM_ACTION_REPEAT
        self._self_collision_enabled = False
        self._init_pos = robot.INIT_POSITION
        self._init_eul = robot.INIT_EUL
        self._init_rack_pos = robot.INIT_RACK_POSITION
        self._init_motor_angle = robot.INIT_MOTOR_ANGLES
        self._motor_direction = robot.JOINT_DIRECTIONS
        self._motor_offset = robot.JOINT_OFFSETS
        self._motor_kp = robot.motor_kp
        self._motor_kd = robot.motor_kd
        self._motor_torque_limits = None
        self._motor_control_mode = robot_motor.POSITION
        self._observed_motor_torques = np.zeros(self.num_motors)
        self._applied_motor_torques = np.zeros(self.num_motors)
        self._max_force = 3.5
        self._pd_latency = 0.0
        self._control_latency = robot.CTRL_LATENCY
        self._observation_noise_stdev = SENSOR_NOISE_STDDEV
        self._observation_history = collections.deque(maxlen=100)
        self._control_observation = []
        self._chassis_link_ids = [-1]
        self._leg_link_ids = []
        self._motor_link_ids = []
        self._foot_link_ids = []
        self._motor_overheat_protection = False
        self._on_rack = False
        self._reset_at_current_position = False
        self.SetAllSensors(
            robot.sensors if robot.sensors is not None else list())
        self._is_safe = True

        self._enable_action_interpolation = True
        self._enable_action_filter = True
        self._filter_action = None
        self._enable_randomizer = True

        if self._on_rack and self._reset_at_current_position:
            raise ValueError("on_rack and reset_at_current_position "
                             "cannot be enabled together")

        if isinstance(self._motor_kp, (collections.Sequence, np.ndarray)):
            self._motor_kps = np.asarray(self._motor_kp)
        else:
            self._motor_kps = np.full(self.num_motors, self._motor_kp)

        if isinstance(self._motor_kd, (collections.Sequence, np.ndarray)):
            self._motor_kds = np.asarray(self._motor_kd)
        else:
            self._motor_kds = np.full(self.num_motors, self._motor_kd)

        self._motor_model = robot_motor.RobotMotorModel(
            kp=self._motor_kp,
            kd=self._motor_kd,
            torque_limits=self._motor_torque_limits,
            motor_control_mode=self._motor_control_mode)

        self._step_counter = 0

        # This also includes the time spent during the Reset motion.
        self._state_action_counter = 0

        if self._enable_action_filter:
            self._action_filter = self._BuildActionFilter()

        super(Quadruped, self).__init__(time_step=self.sim_time_step,
                                        action_repeat=self._action_repeat, enable_randomizer=self._enable_randomizer)

        # self.reset_robot(reload_urdf=False, reset_time=-1.0)
        self.ReceiveObservation()

        return

    def _BuildActionFilter(self):
        sampling_rate = 1 / (self.sim_time_step * self._action_repeat)
        num_joints = self.GetActionDimension()
        a_filter = action_filter.ActionFilterButter(
            sampling_rate=sampling_rate, num_joints=num_joints)
        return a_filter

    def _ResetActionFilter(self):
        self._action_filter.reset()
        return

    def _FilterAction(self, action):
        # initialize the filter history, since resetting the filter will fill
        # the history with zeros and this can cause sudden movements at the start
        # of each episode
        if self._step_counter == 0:
            default_action = self.GetMotorAngles()
            self._action_filter.init_history(default_action)

        filtered_action = self._action_filter.filter(action)
        return filtered_action

    def set_action(self, action):
        """Steps simulation."""
        action += self._init_motor_angle
        self._last_action = action

        self.imitation_step()

        if self._enable_action_filter:
            action = self._FilterAction(action)

        for i in range(self._action_repeat):
            proc_action = self.ProcessAction(action, i)
            self._StepInternal(proc_action)
            self._step_counter += 1

        for s in self._sensors:
            s.on_step()

        observations = self._get_observation()

        self._filter_action = action

        return observations

    def ProcessAction(self, action, substep_count):
        """If enabled, interpolates between the current and previous actions.

        Args:
          action: current action.
          substep_count: the step count should be between [0, self.__action_repeat).

        Returns:
          If interpolation is enabled, returns interpolated action depending on
          the current action repeat substep.
        """
        if self._enable_action_interpolation:
            if self._filter_action is not None:
                prev_action = self._filter_action
            else:
                prev_action = self.GetMotorAngles()

            lerp = float(substep_count + 1) / self._action_repeat
            proc_action = prev_action + lerp * (action - prev_action)
        else:
            proc_action = action

        return proc_action

    def _ClipMotorCommands(self, motor_commands):
        """Clips motor commands.

        Args:
          motor_commands: np.array. Can be motor angles, torques, hybrid commands,
            or motor pwms (for Minitaur only).

        Returns:
          Clipped motor commands.
        """

        # clamp the motor command by the joint limit, in case weired things happens
        max_angle_change = MAX_MOTOR_ANGLE_CHANGE_PER_STEP
        current_motor_angles = self.GetMotorAngles()
        motor_commands = np.clip(motor_commands,
                                 current_motor_angles - max_angle_change,
                                 current_motor_angles + max_angle_change)
        return motor_commands

    def ApplyAction(self, motor_commands, motor_control_mode=None):
        """Apply the motor commands using the motor model.

        Args:
          motor_commands: np.array. Can be motor angles, torques, hybrid commands,
            or motor pwms (for Minitaur only).
          motor_control_mode: A MotorControlMode enum.
        """
        self.last_action_time = self._state_action_counter * self.sim_time_step
        control_mode = motor_control_mode
        if control_mode is None:
            control_mode = self._motor_control_mode

        motor_commands = self._ClipMotorCommands(motor_commands)
        motor_commands = np.asarray(motor_commands)

        q, qdot = self._GetPDObservation()
        qdot_true = self.GetTrueMotorVelocities()
        actual_torque, observed_torque = self._motor_model.convert_to_torque(
            motor_commands, q, qdot, qdot_true, control_mode)

        # May turn off the motor
        self._ApplyOverheatProtection(actual_torque)

        # The torque is already in the observation space because we use
        # GetMotorAngles and GetMotorVelocities.
        self._observed_motor_torques = observed_torque

        # Transform into the motor space when applying the torque.
        self._applied_motor_torque = np.multiply(actual_torque,
                                                 self._motor_direction)
        motor_ids = []
        motor_torques = []

        for motor_id, motor_torque, motor_enabled in zip(self._motor_id_list,
                                                         self._applied_motor_torque,
                                                         self._motor_enabled_list):
            if motor_enabled:
                motor_ids.append(motor_id)
                motor_torques.append(motor_torque)
            else:
                motor_ids.append(motor_id)
                motor_torques.append(0)
        self._SetMotorTorqueByIds(motor_ids, motor_torques)

    def _StepInternal(self, action, motor_control_mode=None):
        self.ApplyAction(action, motor_control_mode)
        self.pybullet_client.stepSimulation()
        self.ReceiveObservation()
        self._state_action_counter += 1

        return

    def ResetPose(self, add_constraint):
        """Reset the pose of the minitaur.

        Args:
          add_constraint: Whether to add a constraint at the joints of two feet.
        """
        del add_constraint
        for name in self._joint_name_to_id:
            joint_id = self._joint_name_to_id[name]
            self.pybullet_client.setJointMotorControl2(
                bodyIndex=self.quadruped,
                jointIndex=(joint_id),
                controlMode=self.pybullet_client.VELOCITY_CONTROL,
                targetVelocity=0,
                force=0)

        for name, i in zip(self.name_motor, range(len(self.name_motor))):
            angle = self._init_motor_angle[i] + self._motor_offset[i]
            self.pybullet_client.resetJointState(
                self.quadruped, self._joint_name_to_id[name], angle, targetVelocity=0)

    def _SettleDownForReset(self, default_motor_angles, reset_time):
        self.ReceiveObservation()
        if reset_time <= 0:
            return
        for _ in range(500):
            self._StepInternal(
                self._init_motor_angle,
                motor_control_mode=robot_motor.POSITION)
        if default_motor_angles is not None:
            num_steps_to_reset = int(reset_time / self.sim_time_step)
            for _ in range(num_steps_to_reset):
                self._StepInternal(
                    default_motor_angles,
                    motor_control_mode=robot_motor.POSITION)

    def reset_robot(self, reload_urdf=True, default_motor_angles=None, reset_time=3.0):
        """Reset the minitaur to its initial states.

        Args:
          reload_urdf: Whether to reload the urdf file. If not, Reset() just place
            the minitaur back to its starting position.
          default_motor_angles: The default motor angles. If it is None, minitaur
            will hold a default pose (motor angle math.pi / 2) for 100 steps. In
            torque control mode, the phase of holding the default pose is skipped.
          reset_time: The duration (in seconds) to hold the default motor angles. If
            reset_time <= 0 or in torque control mode, the phase of holding the
            default pose is skipped.
        """
        if reload_urdf:
            self._LoadRobotURDF()
            if self._on_rack:
                self.rack_constraint = (
                    self._CreateRackConstraint(self.GetDefaultInitPosition(),
                                               self.GetDefaultInitOrientation()))
            self._BuildJointNameToIdDict()
            self._BuildUrdfIds()
            self._RemoveDefaultJointDamping()
            self._BuildMotorIdList()
            self._RecordMassInfoFromURDF()
            self._RecordInertiaInfoFromURDF()
            self.ResetPose(add_constraint=True)
        else:
            self.pybullet_client.resetBasePositionAndOrientation(
                self.quadruped, self.GetDefaultInitPosition(),
                self.GetDefaultInitOrientation())
            self.pybullet_client.resetBaseVelocity(self.quadruped, [0, 0, 0],
                                                   [0, 0, 0])
            self.ResetPose(add_constraint=False)

        self._overheat_counter = np.zeros(self.num_motors)
        self._motor_enabled_list = [True] * self.num_motors
        self._observation_history.clear()
        self._step_counter = 0
        self._state_action_counter = 0
        self._is_safe = True
        self._filter_action = None
        self._last_action = np.zeros(self.action_space.shape)

        self._SettleDownForReset(default_motor_angles, reset_time)

        if self._enable_action_filter:
            self._ResetActionFilter()

        for s in self._sensors:
            s.on_reset()
        observations = self._get_observation()

        return observations

    def _LoadRobotURDF(self):
        """Loads the URDF file for the robot."""
        urdf_file = self.GetURDFFile()
        if self._self_collision_enabled:
            self.quadruped = self.pybullet_client.loadURDF(
                urdf_file,
                self._GetDefaultInitPosition(),
                self.GetDefaultInitOrientation(),
                flags=self.pybullet_client.URDF_USE_SELF_COLLISION)
        else:
            self.quadruped = self.pybullet_client.loadURDF(
                urdf_file, self.GetDefaultInitPosition(),
                self.GetDefaultInitOrientation())

    def _BuildUrdfIds(self):
        """Build the link Ids from its name in the URDF file.

        Raises:
          ValueError: Unknown category of the joint name.
        """
        num_joints = self.pybullet_client.getNumJoints(self.quadruped)
        self._chassis_link_ids = [-1]
        self._leg_link_ids = []
        self._motor_link_ids = []
        self._knee_link_ids = []
        self._foot_link_ids = []

        for i in range(num_joints):
            joint_info = self.pybullet_client.getJointInfo(self.quadruped, i)
            joint_name = joint_info[1].decode("UTF-8")
            joint_id = self._joint_name_to_id[joint_name]
            if self.pattern[0].match(joint_name):
                self._chassis_link_ids.append(joint_id)
            elif self.pattern[1].match(joint_name):
                self._motor_link_ids.append(joint_id)
            # We either treat the lower leg or the toe as the foot link, depending on
            # the urdf version used.
            elif self.pattern[2].match(joint_name):
                self._knee_link_ids.append(joint_id)
            elif self.pattern[3].match(joint_name):
                self._foot_link_ids.append(joint_id)
            else:
                raise ValueError("Unknown category of joint %s" % joint_name)

        self._leg_link_ids.extend(self._knee_link_ids)
        self._leg_link_ids.extend(self._foot_link_ids)
        self._foot_link_ids.extend(self._knee_link_ids)

        self._chassis_link_ids.sort()
        self._motor_link_ids.sort()
        self._foot_link_ids.sort()
        self._leg_link_ids.sort()

        return

    def _RecordMassInfoFromURDF(self):
        """Records the mass information from the URDF file."""
        self._base_mass_urdf = []
        for chassis_id in self._chassis_link_ids:
            self._base_mass_urdf.append(
                self.pybullet_client.getDynamicsInfo(self.quadruped, chassis_id)[0])
        self._leg_masses_urdf = []
        for leg_id in self._leg_link_ids:
            self._leg_masses_urdf.append(
                self.pybullet_client.getDynamicsInfo(self.quadruped, leg_id)[0])
        for motor_id in self._motor_link_ids:
            self._leg_masses_urdf.append(
                self.pybullet_client.getDynamicsInfo(self.quadruped, motor_id)[0])

    def _RecordInertiaInfoFromURDF(self):
        """Record the inertia of each body from URDF file."""
        self._link_urdf = []
        num_bodies = self.pybullet_client.getNumJoints(self.quadruped)
        for body_id in range(-1, num_bodies):  # -1 is for the base link.
            inertia = self.pybullet_client.getDynamicsInfo(self.quadruped,
                                                           body_id)[2]
            self._link_urdf.append(inertia)
        # We need to use id+1 to index self._link_urdf because it has the base
        # (index = -1) at the first element.
        self._base_inertia_urdf = [
            self._link_urdf[chassis_id + 1] for chassis_id in self._chassis_link_ids
        ]
        self._leg_inertia_urdf = [
            self._link_urdf[leg_id + 1] for leg_id in self._leg_link_ids
        ]
        self._leg_inertia_urdf.extend(
            [self._link_urdf[motor_id + 1] for motor_id in self._motor_link_ids])

    def _BuildJointNameToIdDict(self):
        num_joints = self.pybullet_client.getNumJoints(self.quadruped)
        self._joint_name_to_id = {}
        for i in range(num_joints):
            joint_info = self.pybullet_client.getJointInfo(self.quadruped, i)
            self._joint_name_to_id[joint_info[1].decode(
                "UTF-8")] = joint_info[0]

    def _RemoveDefaultJointDamping(self):
        num_joints = self.pybullet_client.getNumJoints(self.quadruped)
        for i in range(num_joints):
            joint_info = self.pybullet_client.getJointInfo(self.quadruped, i)
            self.pybullet_client.changeDynamics(
                joint_info[0], -1, linearDamping=0, angularDamping=0)

    def _BuildMotorIdList(self):
        self._motor_id_list = [
            self._joint_name_to_id[motor_name]
            for motor_name in self._GetMotorNames()
        ]

    def _CreateRackConstraint(self, init_position, init_orientation):
        """Create a constraint that keeps the chassis at a fixed frame.

        This frame is defined by init_position and init_orientation.

        Args:
          init_position: initial position of the fixed frame.
          init_orientation: initial orientation of the fixed frame in quaternion
            format [x,y,z,w].

        Returns:
          Return the constraint id.
        """
        fixed_constraint = self.pybullet_client.createConstraint(
            parentBodyUniqueId=self.quadruped,
            parentLinkIndex=-1,
            childBodyUniqueId=-1,
            childLinkIndex=-1,
            jointType=self.pybullet_client.JOINT_FIXED,
            jointAxis=[0, 0, 0],
            parentFramePosition=[0, 0, 0],
            childFramePosition=init_position,
            childFrameOrientation=init_orientation)
        return fixed_constraint

    def SetBaseMasses(self, base_mass):
        """Set the mass of minitaur's base.

        Args:
          base_mass: A list of masses of each body link in CHASIS_LINK_IDS. The
            length of this list should be the same as the length of CHASIS_LINK_IDS.

        Raises:
          ValueError: It is raised when the length of base_mass is not the same as
            the length of self._chassis_link_ids.
        """
        if len(base_mass) != len(self._chassis_link_ids):
            raise ValueError(
                "The length of base_mass {} and self._chassis_link_ids {} are not "
                "the same.".format(len(base_mass), len(self._chassis_link_ids)))
        for chassis_id, chassis_mass in zip(self._chassis_link_ids, base_mass):
            self.pybullet_client.changeDynamics(
                self.quadruped, chassis_id, mass=chassis_mass)

    def SetLegMasses(self, leg_masses):
        """Set the mass of the legs.

        A leg includes leg_link and motor. 4 legs contain 16 links (4 links each)
        and 8 motors. First 16 numbers correspond to link masses, last 8 correspond
        to motor masses (24 total).

        Args:
          leg_masses: The leg and motor masses for all the leg links and motors.

        Raises:
          ValueError: It is raised when the length of masses is not equal to number
            of links + motors.
        """
        if len(leg_masses) != len(self._leg_link_ids) + len(self._motor_link_ids):
            raise ValueError("The number of values passed to SetLegMasses are "
                             "different than number of leg links and motors.")
        for leg_id, leg_mass in zip(self._leg_link_ids, leg_masses):
            self.pybullet_client.changeDynamics(
                self.quadruped, leg_id, mass=leg_mass)
        motor_masses = leg_masses[len(self._leg_link_ids):]
        for link_id, motor_mass in zip(self._motor_link_ids, motor_masses):
            self.pybullet_client.changeDynamics(
                self.quadruped, link_id, mass=motor_mass)

    def SetBaseInertias(self, base_inertias):
        """Set the inertias of minitaur's base.

        Args:
          base_inertias: A list of inertias of each body link in CHASIS_LINK_IDS.
            The length of this list should be the same as the length of
            CHASIS_LINK_IDS.

        Raises:
          ValueError: It is raised when the length of base_inertias is not the same
            as the length of self._chassis_link_ids and base_inertias contains
            negative values.
        """
        if len(base_inertias) != len(self._chassis_link_ids):
            raise ValueError(
                "The length of base_inertias {} and self._chassis_link_ids {} are "
                "not the same.".format(
                    len(base_inertias), len(self._chassis_link_ids)))
        for chassis_id, chassis_inertia in zip(self._chassis_link_ids,
                                               base_inertias):
            for inertia_value in chassis_inertia:
                if (np.asarray(inertia_value) < 0).any():
                    raise ValueError(
                        "Values in inertia matrix should be non-negative.")
            self.pybullet_client.changeDynamics(
                self.quadruped, chassis_id, localInertiaDiagonal=chassis_inertia)

    def SetLegInertias(self, leg_inertias):
        """Set the inertias of the legs.

        A leg includes leg_link and motor. 4 legs contain 16 links (4 links each)
        and 8 motors. First 16 numbers correspond to link inertia, last 8 correspond
        to motor inertia (24 total).

        Args:
          leg_inertias: The leg and motor inertias for all the leg links and motors.

        Raises:
          ValueError: It is raised when the length of inertias is not equal to
          the number of links + motors or leg_inertias contains negative values.
        """

        if len(leg_inertias) != len(self._leg_link_ids) + len(self._motor_link_ids):
            raise ValueError("The number of values passed to SetLegMasses are "
                             "different than number of leg links and motors.")
        for leg_id, leg_inertia in zip(self._leg_link_ids, leg_inertias):
            for inertia_value in leg_inertias:
                if (np.asarray(inertia_value) < 0).any():
                    raise ValueError(
                        "Values in inertia matrix should be non-negative.")
            self.pybullet_client.changeDynamics(
                self.quadruped, leg_id, localInertiaDiagonal=leg_inertia)

        motor_inertias = leg_inertias[len(self._leg_link_ids):]
        for link_id, motor_inertia in zip(self._motor_link_ids, motor_inertias):
            for inertia_value in motor_inertias:
                if (np.asarray(inertia_value) < 0).any():
                    raise ValueError(
                        "Values in inertia matrix should be non-negative.")
            self.pybullet_client.changeDynamics(
                self.quadruped, link_id, localInertiaDiagonal=motor_inertia)

    def SetFootFriction(self, foot_friction):
        """Set the lateral friction of the feet.

        Args:
          foot_friction: The lateral friction coefficient of the foot. This value is
            shared by all four feet.
        """
        for link_id in self._foot_link_ids:
            self.pybullet_client.changeDynamics(
                self.quadruped, link_id, lateralFriction=foot_friction)

    def SetFootRestitution(self, foot_restitution):
        """Set the coefficient of restitution at the feet.

        Args:
          foot_restitution: The coefficient of restitution (bounciness) of the feet.
            This value is shared by all four feet.
        """
        for link_id in self._foot_link_ids:
            self.pybullet_client.changeDynamics(
                self.quadruped, link_id, restitution=foot_restitution)

    def SetJointFriction(self, joint_frictions):
        for knee_joint_id, friction in zip(self._foot_link_ids, joint_frictions):
            self.pybullet_client.setJointMotorControl2(
                bodyIndex=self.quadruped,
                jointIndex=knee_joint_id,
                controlMode=self.pybullet_client.VELOCITY_CONTROL,
                targetVelocity=0,
                force=friction)

    def ReceiveObservation(self):
        """Receive the observation from sensors.

        This function is called once per step. The observations are only updated
        when this function is called.
        """
        _, self._init_orientation_inv = self.pybullet_client.invertTransform(
            position=[0, 0, 0], orientation=self.GetDefaultInitOrientation())
        self._joint_states = self.pybullet_client.getJointStates(
            self.quadruped, self._motor_id_list)
        self._base_linear_vel, self._base_angular_vel = self.pybullet_client.getBaseVelocity(
            self.quadruped)
        self._base_position, orientation = (
            self.pybullet_client.getBasePositionAndOrientation(self.quadruped))
        # Computes the relative orientation relative to the robot's
        # initial_orientation.
        _, self._base_orientation = self.pybullet_client.multiplyTransforms(
            positionA=[0, 0, 0],
            orientationA=orientation,
            positionB=[0, 0, 0],
            orientationB=self._init_orientation_inv)
        self._observation_history.appendleft(self.GetTrueObservation())
        self._control_observation = self._GetControlObservation()
        self.last_state_time = self._state_action_counter * self.sim_time_step

    def GetTrueObservation(self):
        observation = []
        observation.extend(self.GetTrueMotorAngles())
        observation.extend(self.GetTrueMotorVelocities())
        observation.extend(self.GetTrueMotorTorques())
        observation.extend(self.GetTrueBaseOrientation())
        observation.extend(self.GetTrueBaseRollPitchYawRate())
        return observation

    def _get_observation(self):
        """Get observation of this environment from a list of sensors.

        Returns:
          observations: sensory observation in the numpy array format
        """
        sensors_dict = {}
        for s in self._sensors:
            sensors_dict[s.get_name()] = s.get_observation()

        observations = collections.OrderedDict(
            sorted(list(sensors_dict.items())))
        return observations

    def _GetDelayedObservation(self, latency):
        """Get observation that is delayed by the amount specified in latency.

        Args:
          latency: The latency (in seconds) of the delayed observation.

        Returns:
          observation: The observation which was actually latency seconds ago.
        """
        if latency <= 0 or len(self._observation_history) == 1:
            observation = self._observation_history[0]
        else:
            n_steps_ago = int(latency / self.sim_time_step)
            if n_steps_ago + 1 >= len(self._observation_history):
                return self._observation_history[-1]
            remaining_latency = latency - n_steps_ago * self.sim_time_step
            blend_alpha = remaining_latency / self.sim_time_step
            observation = (
                (1.0 - blend_alpha) *
                np.array(self._observation_history[n_steps_ago])
                + blend_alpha * np.array(self._observation_history[n_steps_ago + 1]))
        return observation

    def _GetPDObservation(self):
        pd_delayed_observation = self._GetDelayedObservation(self._pd_latency)
        q = pd_delayed_observation[0:self.num_motors]
        qdot = pd_delayed_observation[self.num_motors:2 * self.num_motors]
        return (np.array(q), np.array(qdot))

    def _GetControlObservation(self):
        control_delayed_observation = self._GetDelayedObservation(
            self._control_latency)
        return control_delayed_observation

    def SetAllSensors(self, sensors):
        """set all sensors to this robot and move the ownership to this robot.

        Args:
          sensors: a list of sensors to this robot.
        """
        for s in sensors:
            s.set_robot(self)
        self._sensors = sensors

    def GetAllSensors(self):
        """get all sensors associated with this robot.

        Returns:
          sensors: a list of all sensors.
        """
        return self._sensors

    def GetSensor(self, name):
        """get the first sensor with the given name.

        This function return None if a sensor with the given name does not exist.

        Args:
          name: the name of the sensor we are looking

        Returns:
          sensor: a sensor with the given name. None if not exists.
        """
        for s in self._sensors:
            if s.get_name() == name:
                return s
        return None

    def _AddSensorNoise(self, sensor_values, noise_stdev):
        if noise_stdev <= 0:
            return sensor_values
        observation = sensor_values + np.random.normal(
            scale=noise_stdev, size=sensor_values.shape)
        return observation

    def GetTimeSinceReset(self):
        return self._step_counter * self.sim_time_step

    def GetDefaultInitPosition(self):
        """Returns the init position of the robot.

        It can be either 1) origin (INIT_POSITION), 2) origin with a rack
        (INIT_RACK_POSITION), or 3) the previous position.
        """
        if self._on_rack:
            return self._init_rack_pos
        else:
            return self._init_pos

    def GetDefaultInitOrientation(self):
        """Returns the init position of the robot.

        It can be either 1) INIT_ORIENTATION or 2) the previous rotation in yaw.
        """
        return self._init_eul

    def GetBasePosition(self):
        """Get the position of minitaur's base.

        Returns:
          The position of minitaur's base.
        """
        return self._base_position

    def GetBaseVelocity(self):
        """Get the linear velocity of minitaur's base.

        Returns:
          The velocity of minitaur's base.
        """
        return self._base_linear_vel

    def GetTrueBaseRollPitchYaw(self):
        """Get minitaur's base orientation in euler angle in the world frame.

        Returns:
          A tuple (roll, pitch, yaw) of the base in world frame.
        """
        orientation = self.GetTrueBaseOrientation()
        roll_pitch_yaw = transformations.euler_from_quaternion(orientation)
        return np.asarray(roll_pitch_yaw)

    def GetBaseRollPitchYaw(self):
        """Get minitaur's base orientation in euler angle in the world frame.

        This function mimicks the noisy sensor reading and adds latency.
        Returns:
          A tuple (roll, pitch, yaw) of the base in world frame polluted by noise
          and latency.
        """
        delayed_orientation = np.array(
            self._control_observation[3 * self.num_motors:3 * self.num_motors + 4])
        delayed_roll_pitch_yaw = transformations.euler_from_quaternion(
            delayed_orientation)
        roll_pitch_yaw = self._AddSensorNoise(
            np.array(delayed_roll_pitch_yaw), self._observation_noise_stdev[3])
        return roll_pitch_yaw

    def GetDefaultInitJointPose(self):
        """Get default initial joint pose."""
        joint_pose = (self._init_motor_angle +
                      self._motor_offset) * self._motor_direction
        return joint_pose

    def GetTrueMotorAngles(self):
        """Gets the eight motor angles at the current moment, mapped to [-pi, pi].

        Returns:
          Motor angles, mapped to [-pi, pi].
        """
        motor_angles = [state[0] for state in self._joint_states]
        motor_angles = np.multiply(
            np.asarray(motor_angles) - np.asarray(self._motor_offset),
            self._motor_direction)
        return motor_angles

    def GetMotorAngles(self):
        """Gets the eight motor angles.

        This function mimicks the noisy sensor reading and adds latency. The motor
        angles that are delayed, noise polluted, and mapped to [-pi, pi].

        Returns:
          Motor angles polluted by noise and latency, mapped to [-pi, pi].
        """
        motor_angles = self._AddSensorNoise(
            np.array(self._control_observation[0:self.num_motors]),
            self._observation_noise_stdev[0])
        # map to pi2pi
        mapped_angles = copy.deepcopy(motor_angles)
        for i in range(len(motor_angles)):
            mapped_angles[i] = math.fmod(motor_angles[i], TWO_PI)
            if mapped_angles[i] >= math.pi:
                mapped_angles[i] -= TWO_PI
            elif mapped_angles[i] < -math.pi:
                mapped_angles[i] += TWO_PI
        return mapped_angles

    def GetTrueMotorVelocities(self):
        """Get the velocity of all eight motors.

        Returns:
          Velocities of all eight motors.
        """
        motor_velocities = [state[1] for state in self._joint_states]

        motor_velocities = np.multiply(motor_velocities, self._motor_direction)
        return motor_velocities

    def GetMotorVelocities(self):
        """Get the velocity of all eight motors.

        This function mimicks the noisy sensor reading and adds latency.
        Returns:
          Velocities of all eight motors polluted by noise and latency.
        """
        return self._AddSensorNoise(
            np.array(self._control_observation[self.num_motors:2 *
                                               self.num_motors]),
            self._observation_noise_stdev[1])

    def GetTrueMotorTorques(self):
        """Get the amount of torque the motors are exerting.

        Returns:
          Motor torques of all eight motors.
        """
        return self._observed_motor_torques

    def GetMotorTorques(self):
        """Get the amount of torque the motors are exerting.

        This function mimicks the noisy sensor reading and adds latency.
        Returns:
          Motor torques of all eight motors polluted by noise and latency.
        """
        return self._AddSensorNoise(
            np.array(self._control_observation[2 * self.num_motors:3 *
                                               self.num_motors]),
            self._observation_noise_stdev[2])

    def GetEnergyConsumptionPerControlStep(self):
        """Get the amount of energy used in last one time step.

        Returns:
          Energy Consumption based on motor velocities and torques (Nm^2/s).
        """
        return np.abs(np.dot(
            self.GetMotorTorques(),
            self.GetMotorVelocities())) * self.sim_time_step * self._action_repeat

    def GetTrueBaseOrientation(self):
        """Get the orientation of minitaur's base, represented as quaternion.

        Returns:
          The orientation of minitaur's base.
        """
        return self._base_orientation

    def GetBaseOrientation(self):
        """Get the orientation of minitaur's base, represented as quaternion.

        This function mimicks the noisy sensor reading and adds latency.
        Returns:
          The orientation of minitaur's base polluted by noise and latency.
        """
        return transformations.quaternion_from_euler(self.GetBaseRollPitchYaw())

    def GetTrueBaseRollPitchYawRate(self):
        """Get the rate of orientation change of the minitaur's base in euler angle.

        Returns:
          rate of (roll, pitch, yaw) change of the minitaur's base.
        """
        angular_velocity = self.pybullet_client.getBaseVelocity(self.quadruped)[
            1]
        orientation = self.GetTrueBaseOrientation()
        return self.TransformAngularVelocityToLocalFrame(angular_velocity,
                                                         orientation)

    def TransformAngularVelocityToLocalFrame(self, angular_velocity, orientation):
        """Transform the angular velocity from world frame to robot's frame.

        Args:
          angular_velocity: Angular velocity of the robot in world frame.
          orientation: Orientation of the robot represented as a quaternion.

        Returns:
          angular velocity of based on the given orientation.
        """
        # Treat angular velocity as a position vector, then transform based on the
        # orientation given by dividing (or multiplying with inverse).
        # Get inverse quaternion assuming the vector is at 0,0,0 origin.
        _, orientation_inversed = self.pybullet_client.invertTransform([0, 0, 0],
                                                                       orientation)
        # Transform the angular_velocity at neutral orientation using a neutral
        # translation and reverse of the given orientation.
        relative_velocity, _ = self.pybullet_client.multiplyTransforms(
            [0, 0, 0], orientation_inversed, angular_velocity,
            transformations.quaternion_from_euler([0, 0, 0]))
        return np.asarray(relative_velocity)

    def GetBaseRollPitchYawRate(self):
        """Get the rate of orientation change of the minitaur's base in euler angle.

        This function mimicks the noisy sensor reading and adds latency.
        Returns:
          rate of (roll, pitch, yaw) change of the minitaur's base polluted by noise
          and latency.
        """
        return self._AddSensorNoise(
            np.array(self._control_observation[3 * self.num_motors +
                                               4:3 * self.num_motors + 7]),
            self._observation_noise_stdev[4])

    def GetFootLinkIDs(self):
        """Get list of IDs for all foot links."""
        return self._foot_link_ids

    def _SetMotorTorqueById(self, motor_id, torque):
        self.pybullet_client.setJointMotorControl2(
            bodyIndex=self.quadruped,
            jointIndex=motor_id,
            controlMode=self.pybullet_client.TORQUE_CONTROL,
            force=torque)

    def _SetMotorTorqueByIds(self, motor_ids, torques):
        self.pybullet_client.setJointMotorControlArray(
            bodyIndex=self.quadruped,
            jointIndices=motor_ids,
            controlMode=self.pybullet_client.TORQUE_CONTROL,
            forces=torques)

    def GetURDFFile(self):
        return self._urdf_file

    def GetActionDimension(self):
        """Get the length of the action list.

        Returns:
          The length of the action list.
        """
        return self.num_motors

    def _ApplyOverheatProtection(self, actual_torque):
        if self._motor_overheat_protection:
            for i in range(self.num_motors):
                if abs(actual_torque[i]) > OVERHEAT_SHUTDOWN_TORQUE:
                    self._overheat_counter[i] += 1
                else:
                    self._overheat_counter[i] = 0
                if (self._overheat_counter[i] >
                        OVERHEAT_SHUTDOWN_TIME / self.sim_time_step):
                    self._motor_enabled_list[i] = False

    def GetBaseMassesFromURDF(self):
        """Get the mass of the base from the URDF file."""
        return self._base_mass_urdf

    def GetBaseInertiasFromURDF(self):
        """Get the inertia of the base from the URDF file."""
        return self._base_inertia_urdf

    def GetLegMassesFromURDF(self):
        """Get the mass of the legs from the URDF file."""
        return self._leg_masses_urdf

    def GetLegInertiasFromURDF(self):
        """Get the inertia of the legs from the URDF file."""
        return self._leg_inertia_urdf

    def GetNumKneeJoints(self):
        return len(self._foot_link_ids)

    def SetBatteryVoltage(self, voltage):
        self._motor_model.set_voltage(voltage)

    def SetMotorViscousDamping(self, viscous_damping):
        self._motor_model.set_viscous_damping(viscous_damping)

    def SetControlLatency(self, latency):
        """Set the latency of the control loop.

        It measures the duration between sending an action from Nvidia TX2 and
        receiving the observation from microcontroller.

        Args:
          latency: The latency (in seconds) of the control loop.
        """
        self._control_latency = latency

    def GetControlLatency(self):
        """Get the control latency.

        Returns:
          The latency (in seconds) between when the motor command is sent and when
            the sensor measurements are reported back to the controller.
        """
        return self._control_latency

    def SetMotorGains(self, kp, kd):
        """Set the gains of all motors.

        These gains are PD gains for motor positional control. kp is the
        proportional gain and kd is the derivative gain.

        Args:
          kp: proportional gain(s) of the motors.
          kd: derivative gain(s) of the motors.
        """
        if isinstance(kp, (collections.Sequence, np.ndarray)):
            self._motor_kps = np.asarray(kp)
        else:
            self._motor_kps = np.full(self.num_motors, kp)

        if isinstance(kd, (collections.Sequence, np.ndarray)):
            self._motor_kds = np.asarray(kd)
        else:
            self._motor_kds = np.full(self.num_motors, kd)

        self._motor_model.set_motor_gains(kp, kd)

    def GetMotorGains(self):
        """Get the gains of the motor.

        Returns:
          The proportional gain.
          The derivative gain.
        """
        return self._motor_kps, self._motor_kds

    def GetMotorPositionGains(self):
        """Get the position gains of the motor.

        Returns:
          The proportional gain.
        """
        return self._motor_kps

    def GetMotorVelocityGains(self):
        """Get the velocity gains of the motor.

        Returns:
          The derivative gain.
        """
        return self._motor_kds

    def SetMotorStrengthRatio(self, ratio):
        """Set the strength of all motors relative to the default value.

        Args:
          ratio: The relative strength. A scalar range from 0.0 to 1.0.
        """
        self._motor_model.set_strength_ratios([ratio] * self.num_motors)

    def SetMotorStrengthRatios(self, ratios):
        """Set the strength of each motor relative to the default value.

        Args:
          ratios: The relative strength. A numpy array ranging from 0.0 to 1.0.
        """
        self._motor_model.set_strength_ratios(ratios)

    def _GetMotorNames(self):
        return self.name_motor

    @property
    def is_safe(self):
        return self._is_safe

    @property
    def last_action(self):
        return self._last_action

    @property
    def joint_states(self):
        return self._joint_states

    def imitation_step(self):
        raise NotImplementedError()

    def Terminate(self):
        raise NotImplementedError()
