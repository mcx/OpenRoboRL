# coding=utf-8
# Copyright 2020 The Google Research Authors.
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
from gym import spaces

from envs.quadruped_robot.robots import minitaur_motor
from envs.utilities import action_filter
from envs.utilities.sensors import sensor
from envs.utilities.sensors import environment_sensors
from envs.utilities.sensors import sensor_wrappers
from envs.utilities.sensors import robot_sensors
from envs.utilities.randomizer import controllable_env_randomizer_from_config
from envs.utilities import pose3d


class Minitaur(object):
    """The minitaur class that simulates a quadruped robot from Ghost Robotics."""

    def __init__(self, name_robot, robot_index=0, enable_randomizer=True):
        """Constructs a minitaur and reset it to the initial states.

        Args:
          pybullet_client: The instance of BulletClient to manage different
            simulations.
          num_motors: The number of the motors on the robot.
          dofs_per_leg: The number of degrees of freedom for each leg.
          time_step: The time step of the simulation.
          action_repeat: The number of apply_action() for each control step.
          self_collision_enabled: Whether to enable self collision.
          motor_control_mode: Enum. Can either be POSITION, TORQUE, or HYBRID.
          motor_model_class: We can choose from simple pd model to more accureate DC
            motor models.
          motor_kp: proportional gain for the motors.
          motor_kd: derivative gain for the motors.
          motor_torque_limits: Torque limits for the motors. Can be a single float
            or a list of floats specifying different limits for different robots. If
            not provided, the default limit of the robot is used.
          pd_latency: The latency of the observations (in seconds) used to calculate
            PD control. On the real hardware, it is the latency between the
            microcontroller and the motor controller.
          control_latency: The latency of the observations (in second) used to
            calculate action. On the real hardware, it is the latency from the motor
            controller, the microcontroller to the host (Nvidia TX2).
          observation_noise_stdev: The standard deviation of a Gaussian noise model
            for the sensor. It should be an array for separate sensors in the
            following order [motor_angle, motor_velocity, motor_torque,
            base_roll_pitch_yaw, base_angular_velocity]
          motor_overheat_protection: Whether to shutdown the motor that has exerted
            large torque (OVERHEAT_SHUTDOWN_TORQUE) for an extended amount of time
            (OVERHEAT_SHUTDOWN_TIME). See apply_action() in minitaur.py for more
            details.
          motor_direction: A list of direction values, either 1 or -1, to compensate
            the axis difference of motors between the simulation and the real robot.
          motor_offset: A list of offset value for the motor angles. This is used to
            compensate the angle difference between the simulation and the real
            robot.
          on_rack: Whether to place the minitaur on rack. This is only used to debug
            the walking gait. In this mode, the minitaur's base is hanged midair so
            that its walking gait is clearer to visualize.
          reset_at_current_position: Whether to reset the minitaur at the current
            position and orientation. This is for simulating the reset behavior in
            the real world.
          sensors: a list of sensors that are attached to the robot.
          enable_action_interpolation: Whether to interpolate the current action
            with the previous action in order to produce smoother motions
          enable_action_filter: Boolean specifying if a lowpass filter should be
            used to smooth actions.
        """
        if name_robot == "laikago":
            from envs.quadruped_robot.robots import laikago as robot
        elif name_robot == "mini_cheetah":
            from envs.quadruped_robot.robots import mini_cheetah as robot
        else:
            raise ValueError("wrong robot select")

        self._action_repeat = robot.NUM_ACTION_REPEAT
        self.time_step = robot.T_STEP
        self._urdf_file = robot.URDF_FILENAME
        self.num_motors = robot.NUM_MOTORS
        self.name_motor = robot.MOTOR_NAMES
        self.pattern = robot.PATTERN
        self._init_pos = robot.INIT_POSITION
        self._init_quat = robot.INIT_QUAT
        self._init_motor_angle = robot.INIT_MOTOR_ANGLES
        self._motor_direction = robot.JOINT_DIRECTIONS
        self._motor_offset = robot.JOINT_OFFSETS
        self._control_latency = robot.CTRL_LATENCY
        self._motor_kp = robot.motor_kp
        self._motor_kd = robot.motor_kd
        self._motor_control_mode = minitaur_motor.POSITION
        self._overheat_shutdown_tau = robot.OVERHEAT_SHUTDOWN_TORQUE
        self._overheat_shutdown_time = robot.OVERHEAT_SHUTDOWN_TIME
        self._max_motor_angle_step = robot.MAX_MOTOR_ANGLE_CHANGE_PER_STEP

        self._enable_randomizer = enable_randomizer
        self._robot_index = robot_index
        self.num_legs = self.num_motors // robot.DOFS_PER_LEG
        self._self_collision_enabled = False
        self._observed_motor_torques = np.zeros(self.num_motors)
        self._applied_motor_torques = np.zeros(self.num_motors)
        self._max_force = 3.5
        self._pd_latency = 0.0
        self._observation_noise_stdev = (0.0, 0.0, 0.0, 0.0, 0.0)
        self._observation_history = collections.deque(maxlen=100)
        self._control_observation = []
        self._chassis_link_ids = [-1]
        self._leg_link_ids = []
        self._motor_link_ids = []
        self._foot_link_ids = []
        self._motor_overheat_protection = False
        self._on_rack = False
        self._reset_at_current_position = False

        sensor = [sensor_wrappers.HistoricSensorWrapper(
            wrapped_sensor=robot_sensors.MotorAngleSensor(num_motors=self.num_motors), num_history=3),
            sensor_wrappers.HistoricSensorWrapper(
            wrapped_sensor=robot_sensors.IMUSensor(), num_history=3),
            sensor_wrappers.HistoricSensorWrapper(
            wrapped_sensor=environment_sensors.LastActionSensor(num_actions=self.num_motors), num_history=3)]
        self.set_all_sensors(sensor)

        self.action_space = spaces.Box(
            np.array([-2*math.pi]*12),
            np.array([2*math.pi]*12),
            dtype=np.float32)
        gym_space_dict = {}
        for s in self.get_all_sensors():
            gym_space_dict[s.get_name()] = spaces.Box(
                np.array(s.get_lower_bound()),
                np.array(s.get_upper_bound()),
                dtype=np.float32)
        self.observation_space = spaces.Dict(gym_space_dict)

        self._is_safe = True
        self._motor_torque_limits = None

        self._enable_action_interpolation = True
        self._enable_action_filter = True
        self._filter_action = None
        self._action = np.zeros(self.num_motors)
        self._last_action = np.zeros(self.num_motors)

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

        self._motor_model = minitaur_motor.MotorModel(
            kp=self._motor_kp,
            kd=self._motor_kd,
            torque_limits=self._motor_torque_limits,
            motor_control_mode=self._motor_control_mode)

        self.step_counter = 0

        # This also includes the time spent during the Reset motion.
        self._state_action_counter = 0

        if self._enable_action_filter:
            self._action_filter = self._build_action_filter()

        self._randomizers = []
        randomizer = controllable_env_randomizer_from_config.ControllableEnvRandomizerFromConfig(
            verbose=False)
        self._randomizers.append(randomizer)

        return

    def init_robot(self, sim_handler):
        self._pybullet_client = sim_handler
        self._load_urdf(self._robot_index)
        if self._on_rack:
            self.rack_constraint = (
                self._create_rack_constraint(self.get_default_init_pos(),
                                           self.get_default_init_ori()))
        self._build_joint_name_to_dict()
        self._build_urdf_ids()
        self._remove_default_joint_damping()
        self._build_motor_id_list()
        self._record_mass_from_urdf()
        self._record_inertia_from_urdf()
        self.reset_pose(add_constraint=True)

        self._overheat_counter = np.zeros(self.num_motors)
        self._motor_enabled_list = [True] * self.num_motors
        self._observation_history.clear()
        self.step_counter = 0
        self._state_action_counter = 0
        self._is_safe = True
        self._filter_action = None
        self._last_action = np.zeros(self.num_motors)

        self.receive_obs()

        if self._enable_action_filter:
            self._reset_action_filter()

        return

    def reset(self):
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

        pos = copy.deepcopy(self.get_default_init_pos())
        pos[0] -= int(self._robot_index/4)*2
        pos[1] += (self._robot_index%4)*2
        ori = self.get_default_init_ori()
        self._pybullet_client.resetBasePositionAndOrientation(
            self.quadruped, pos, ori)
        self._pybullet_client.resetBaseVelocity(self.quadruped, [0, 0, 0],
                                                [0, 0, 0])
        self.reset_pose(add_constraint=False)

        self._overheat_counter = np.zeros(self.num_motors)
        self._motor_enabled_list = [True] * self.num_motors
        self._observation_history.clear()
        self.step_counter = 0
        self._state_action_counter = 0
        self._is_safe = True
        self._filter_action = None
        self._last_action = np.zeros(self.num_motors)

        self.receive_obs()

        if self._enable_action_filter:
            self._reset_action_filter()

        for s in self.get_all_sensors():
            s.on_reset(self)

        # Loop over all env randomizers.
        if self._enable_randomizer:
            for env_randomizer in self._randomizers:
                env_randomizer.randomize_env(self)

        return

    def set_act(self, action):
        action += self._init_motor_angle
        self._last_action = action
        if self._enable_action_filter:
            self._action = self._filter(action)
        return

    def robot_step(self, i):
        proc_action = self.process_action(self._action, i)
        self.apply_action(proc_action)
        self._state_action_counter += 1
        if i == self._action_repeat-1:
            self._filter_action = self._action
            self.step_counter += 1

    def get_obs(self):
        for s in self.get_all_sensors():
            s.on_step()
        obs = self._get_observation()
        return obs

    def terminate(self):
        pass

    def get_true_obs(self):
        observation = []
        observation.extend(self.get_true_motor_angles())
        observation.extend(self.get_true_motor_vel())
        observation.extend(self.get_true_motor_tau())
        observation.extend(self.get_true_base_orientation())
        observation.extend(self.get_true_base_rpy_rate())
        return observation

    def receive_obs(self):
        """Receive the observation from sensors.

        This function is called once per step. The observations are only updated
        when this function is called.
        """
        self._joint_states = self._pybullet_client.getJointStates(
            self.quadruped, self._motor_id_list)
        self._base_position, orientation = (
            self._pybullet_client.getBasePositionAndOrientation(self.quadruped))
        # Computes the relative orientation relative to the robot's
        # initial_orientation.
        _, _init_orientation_inv = self._pybullet_client.invertTransform(
            position=[0, 0, 0], orientation=self.get_default_init_ori())
        _, self._base_orientation = self._pybullet_client.multiplyTransforms(
            positionA=[0, 0, 0],
            orientationA=orientation,
            positionB=[0, 0, 0],
            orientationB=_init_orientation_inv)
        self._observation_history.appendleft(self.get_true_obs())
        self._control_observation = self._get_ctrl_obs()
        self.last_state_time = self._state_action_counter * self.time_step

    def _get_delay_obs(self, latency):
        """Get observation that is delayed by the amount specified in latency.

        Args:
          latency: The latency (in seconds) of the delayed observation.

        Returns:
          observation: The observation which was actually latency seconds ago.
        """
        if latency <= 0 or len(self._observation_history) == 1:
            observation = self._observation_history[0]
        else:
            n_steps_ago = int(latency / self.time_step)
            if n_steps_ago + 1 >= len(self._observation_history):
                return self._observation_history[-1]
            remaining_latency = latency - n_steps_ago * self.time_step
            blend_alpha = remaining_latency / self.time_step
            observation = (
                (1.0 - blend_alpha) *
                np.array(self._observation_history[n_steps_ago])
                + blend_alpha * np.array(self._observation_history[n_steps_ago + 1]))
        return observation

    def _get_pd_obs(self):
        pd_delayed_observation = self._get_delay_obs(self._pd_latency)
        q = pd_delayed_observation[0:self.num_motors]
        qdot = pd_delayed_observation[self.num_motors:2 * self.num_motors]
        return (np.array(q), np.array(qdot))

    def _get_ctrl_obs(self):
        control_delayed_observation = self._get_delay_obs(
            self._control_latency)
        return control_delayed_observation

    def _add_sensor_noise(self, sensor_values, noise_stdev):
        if noise_stdev <= 0:
            return sensor_values
        observation = sensor_values + np.random.normal(
            scale=noise_stdev, size=sensor_values.shape)
        return observation

    def set_ctrl_latency(self, latency):
        """Set the latency of the control loop.

        It measures the duration between sending an action from Nvidia TX2 and
        receiving the observation from microcontroller.

        Args:
          latency: The latency (in seconds) of the control loop.
        """
        self._control_latency = latency

    def get_ctrl_latency(self):
        """Get the control latency.

        Returns:
          The latency (in seconds) between when the motor command is sent and when
            the sensor measurements are reported back to the controller.
        """
        return self._control_latency

    def get_time_since_reset(self):
        return self._state_action_counter * self.time_step

    def get_foot_link_ids(self):
        """Get list of IDs for all foot links."""
        return self._foot_link_ids

    def set_all_sensors(self, sensors):
        """set all sensors to this robot and move the ownership to this robot.

        Args:
          sensors: a list of sensors to this robot.
        """
        for s in sensors:
            s.set_robot(self)
        self._sensors = sensors

    def get_all_sensors(self):
        """get all sensors associated with this robot.

        Returns:
          sensors: a list of all sensors.
        """
        return self._sensors

    def get_sensor(self, name):
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

    def process_action(self, action, substep_count):
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
                prev_action = self.get_motor_angles()

            lerp = float(substep_count + 1) / self._action_repeat
            proc_action = prev_action + lerp * (action - prev_action)
        else:
            proc_action = action

        return proc_action

    def get_urdf_file(self):
        return self._urdf_file

    def reset_pose(self, add_constraint):
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

    def get_base_pos(self):
        """Get the position of minitaur's base.

        Returns:
          The position of minitaur's base.
        """
        return self._base_position

    def get_base_vel(self):
        """Get the linear velocity of minitaur's base.

        Returns:
          The velocity of minitaur's base.
        """
        velocity, _ = self._pybullet_client.getBaseVelocity(self.quadruped)
        return velocity

    def get_true_base_rpy(self):
        """Get minitaur's base orientation in euler angle in the world frame.

        Returns:
          A tuple (roll, pitch, yaw) of the base in world frame.
        """
        orientation = self.get_true_base_orientation()
        roll_pitch_yaw = self._pybullet_client.getEulerFromQuaternion(
            orientation)
        return np.asarray(roll_pitch_yaw)

    def get_base_rpy(self):
        """Get minitaur's base orientation in euler angle in the world frame.

        This function mimicks the noisy sensor reading and adds latency.
        Returns:
          A tuple (roll, pitch, yaw) of the base in world frame polluted by noise
          and latency.
        """
        delayed_orientation = np.array(
            self._control_observation[3 * self.num_motors:3 * self.num_motors + 4])
        delayed_roll_pitch_yaw = self._pybullet_client.getEulerFromQuaternion(
            delayed_orientation)
        roll_pitch_yaw = self._add_sensor_noise(
            np.array(delayed_roll_pitch_yaw), self._observation_noise_stdev[3])
        return roll_pitch_yaw

    def _get_observation(self):
        """Get observation of this environment from a list of sensors.

        Returns:
          observations: sensory observation in the numpy array format
        """
        sensors_dict = {}
        for s in self.get_all_sensors():
            sensors_dict[s.get_name()] = s.get_observation()

        observations = collections.OrderedDict(
            sorted(list(sensors_dict.items())))
        return observations

    def get_true_motor_angles(self):
        """Gets the eight motor angles at the current moment, mapped to [-pi, pi].

        Returns:
          Motor angles, mapped to [-pi, pi].
        """
        motor_angles = [state[0] for state in self._joint_states]
        motor_angles = np.multiply(
            np.asarray(motor_angles) - np.asarray(self._motor_offset),
            self._motor_direction)
        return motor_angles

    def get_motor_angles(self):
        """Gets the eight motor angles.

        This function mimicks the noisy sensor reading and adds latency. The motor
        angles that are delayed, noise polluted, and mapped to [-pi, pi].

        Returns:
          Motor angles polluted by noise and latency, mapped to [-pi, pi].
        """
        motor_angles = self._add_sensor_noise(
            np.array(self._control_observation[0:self.num_motors]),
            self._observation_noise_stdev[0])
        return pose3d.MapToMinusPiToPi(motor_angles)

    def get_true_motor_vel(self):
        """Get the velocity of all eight motors.

        Returns:
          Velocities of all eight motors.
        """
        motor_velocities = [state[1] for state in self._joint_states]

        motor_velocities = np.multiply(motor_velocities, self._motor_direction)
        return motor_velocities

    def get_motor_vel(self):
        """Get the velocity of all eight motors.

        This function mimicks the noisy sensor reading and adds latency.
        Returns:
          Velocities of all eight motors polluted by noise and latency.
        """
        return self._add_sensor_noise(
            np.array(self._control_observation[self.num_motors:2 *
                                               self.num_motors]),
            self._observation_noise_stdev[1])

    def get_true_motor_tau(self):
        """Get the amount of torque the motors are exerting.

        Returns:
          Motor torques of all eight motors.
        """
        return self._observed_motor_torques

    def get_motor_tau(self):
        """Get the amount of torque the motors are exerting.

        This function mimicks the noisy sensor reading and adds latency.
        Returns:
          Motor torques of all eight motors polluted by noise and latency.
        """
        return self._add_sensor_noise(
            np.array(self._control_observation[2 * self.num_motors:3 *
                                               self.num_motors]),
            self._observation_noise_stdev[2])

    def get_energy_consumption_per_step(self):
        """Get the amount of energy used in last one time step.

        Returns:
          Energy Consumption based on motor velocities and torques (Nm^2/s).
        """
        return np.abs(np.dot(
            self.get_motor_tau(),
            self.get_motor_vel())) * self.time_step * self._action_repeat

    def get_true_base_orientation(self):
        """Get the orientation of minitaur's base, represented as quaternion.

        Returns:
          The orientation of minitaur's base.
        """
        return self._base_orientation

    def get_base_orientation(self):
        """Get the orientation of minitaur's base, represented as quaternion.

        This function mimicks the noisy sensor reading and adds latency.
        Returns:
          The orientation of minitaur's base polluted by noise and latency.
        """
        return self._pybullet_client.getQuaternionFromEuler(
            self.get_base_rpy())

    def get_true_base_rpy_rate(self):
        """Get the rate of orientation change of the minitaur's base in euler angle.

        Returns:
          rate of (roll, pitch, yaw) change of the minitaur's base.
        """
        angular_velocity = self._pybullet_client.getBaseVelocity(self.quadruped)[
            1]
        orientation = self.get_true_base_orientation()
        return self.trans_from_angular_vel_local_frame(angular_velocity,
                                                         orientation)

    def trans_from_angular_vel_local_frame(self, angular_velocity, orientation):
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
        _, orientation_inversed = self._pybullet_client.invertTransform([0, 0, 0],
                                                                        orientation)
        # Transform the angular_velocity at neutral orientation using a neutral
        # translation and reverse of the given orientation.
        relative_velocity, _ = self._pybullet_client.multiplyTransforms(
            [0, 0, 0], orientation_inversed, angular_velocity,
            self._pybullet_client.getQuaternionFromEuler([0, 0, 0]))
        return np.asarray(relative_velocity)

    def get_base_rpy_rate(self):
        """Get the rate of orientation change of the minitaur's base in euler angle.

        This function mimicks the noisy sensor reading and adds latency.
        Returns:
          rate of (roll, pitch, yaw) change of the minitaur's base polluted by noise
          and latency.
        """
        return self._add_sensor_noise(
            np.array(self._control_observation[3 * self.num_motors +
                                               4:3 * self.num_motors + 7]),
            self._observation_noise_stdev[4])

    def get_action_dim(self):
        """Get the length of the action list.

        Returns:
          The length of the action list.
        """
        return self.num_motors

    def _apply_overheat_protection(self, actual_torque):
        if self._motor_overheat_protection:
            for i in range(self.num_motors):
                if abs(actual_torque[i]) > self._overheat_shutdown_tau:
                    self._overheat_counter[i] += 1
                else:
                    self._overheat_counter[i] = 0
                if (self._overheat_counter[i] >
                        self._overheat_shutdown_time / self.time_step):
                    self._motor_enabled_list[i] = False

    def _clip_motor_commands(self, motor_commands):
        """Clips motor commands.

        Args:
          motor_commands: np.array. Can be motor angles, torques, hybrid commands,
            or motor pwms (for Minitaur only).

        Returns:
          Clipped motor commands.
        """

        # clamp the motor command by the joint limit, in case weired things happens
        max_angle_change = self._max_motor_angle_step
        current_motor_angles = self.get_motor_angles()
        motor_commands = np.clip(motor_commands,
                                 current_motor_angles - max_angle_change,
                                 current_motor_angles + max_angle_change)
        return motor_commands

    def apply_action(self, motor_commands, motor_control_mode=None):
        """Apply the motor commands using the motor model.

        Args:
          motor_commands: np.array. Can be motor angles, torques, hybrid commands,
            or motor pwms (for Minitaur only).
          motor_control_mode: A MotorControlMode enum.
        """
        motor_commands = self._clip_motor_commands(motor_commands)

        self.last_action_time = self._state_action_counter * self.time_step
        control_mode = motor_control_mode
        if control_mode is None:
            control_mode = self._motor_control_mode

        motor_commands = np.asarray(motor_commands)

        q, qdot = self._get_pd_obs()
        qdot_true = self.get_true_motor_vel()
        actual_torque, observed_torque = self._motor_model.convert_to_torque(
            motor_commands, q, qdot, qdot_true, control_mode)

        # May turn off the motor
        self._apply_overheat_protection(actual_torque)

        # The torque is already in the observation space because we use
        # get_motor_angles and get_motor_vel.
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
        self._set_motor_tau_by_ids(motor_ids, motor_torques)

    def _record_mass_from_urdf(self):
        """Records the mass information from the URDF file."""
        self._base_mass_urdf = []
        for chassis_id in self._chassis_link_ids:
            self._base_mass_urdf.append(
                self._pybullet_client.getDynamicsInfo(self.quadruped, chassis_id)[0])
        self._leg_masses_urdf = []
        for leg_id in self._leg_link_ids:
            self._leg_masses_urdf.append(
                self._pybullet_client.getDynamicsInfo(self.quadruped, leg_id)[0])
        for motor_id in self._motor_link_ids:
            self._leg_masses_urdf.append(
                self._pybullet_client.getDynamicsInfo(self.quadruped, motor_id)[0])

    def _record_inertia_from_urdf(self):
        """Record the inertia of each body from URDF file."""
        self._link_urdf = []
        num_bodies = self._pybullet_client.getNumJoints(self.quadruped)
        for body_id in range(-1, num_bodies):  # -1 is for the base link.
            inertia = self._pybullet_client.getDynamicsInfo(self.quadruped,
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

    def _build_joint_name_to_dict(self):
        num_joints = self._pybullet_client.getNumJoints(self.quadruped)
        self._joint_name_to_id = {}
        for i in range(num_joints):
            joint_info = self._pybullet_client.getJointInfo(self.quadruped, i)
            self._joint_name_to_id[joint_info[1].decode(
                "UTF-8")] = joint_info[0]

    def _build_urdf_ids(self):
        """Build the link Ids from its name in the URDF file.

        Raises:
          ValueError: Unknown category of the joint name.
        """
        num_joints = self._pybullet_client.getNumJoints(self.quadruped)
        self._chassis_link_ids = [-1]
        self._leg_link_ids = []
        self._motor_link_ids = []
        self._knee_link_ids = []
        self._foot_link_ids = []

        for i in range(num_joints):
            joint_info = self._pybullet_client.getJointInfo(self.quadruped, i)
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

    def _remove_default_joint_damping(self):
        num_joints = self._pybullet_client.getNumJoints(self.quadruped)
        for i in range(num_joints):
            joint_info = self._pybullet_client.getJointInfo(self.quadruped, i)
            self._pybullet_client.changeDynamics(
                joint_info[0], -1, linearDamping=0, angularDamping=0)

    def _build_motor_id_list(self):
        self._motor_id_list = [
            self._joint_name_to_id[motor_name]
            for motor_name in self._get_motor_names()
        ]

    def _create_rack_constraint(self, init_position, init_orientation):
        """Create a constraint that keeps the chassis at a fixed frame.

        This frame is defined by init_position and init_orientation.

        Args:
          init_position: initial position of the fixed frame.
          init_orientation: initial orientation of the fixed frame in quaternion
            format [x,y,z,w].

        Returns:
          Return the constraint id.
        """
        fixed_constraint = self._pybullet_client.createConstraint(
            parentBodyUniqueId=self.quadruped,
            parentLinkIndex=-1,
            childBodyUniqueId=-1,
            childLinkIndex=-1,
            jointType=self._pybullet_client.JOINT_FIXED,
            jointAxis=[0, 0, 0],
            parentFramePosition=[0, 0, 0],
            childFramePosition=init_position,
            childFrameOrientation=init_orientation)
        return fixed_constraint

    def _load_urdf(self, robot_index=0):
        laikago_urdf_path = self.get_urdf_file()
        pos = copy.deepcopy(self.get_default_init_pos())
        pos[0] -= int(robot_index/4)*2
        pos[1] += (robot_index%4)*2
        ori = self.get_default_init_ori()
        if self._self_collision_enabled:
            self.quadruped = self._pybullet_client.loadURDF(
                laikago_urdf_path, pos, ori,
                flags=self._pybullet_client.URDF_USE_SELF_COLLISION)
        else:
            self.quadruped = self._pybullet_client.loadURDF(
                laikago_urdf_path, pos, ori)

    def _set_motor_tau_by_id(self, motor_id, torque):
        self._pybullet_client.setJointMotorControl2(
            bodyIndex=self.quadruped,
            jointIndex=motor_id,
            controlMode=self._pybullet_client.TORQUE_CONTROL,
            force=torque)

    def _set_motor_tau_by_ids(self, motor_ids, torques):
        self._pybullet_client.setJointMotorControlArray(
            bodyIndex=self.quadruped,
            jointIndices=motor_ids,
            controlMode=self._pybullet_client.TORQUE_CONTROL,
            forces=torques)

    def get_base_mass_from_urdf(self):
        """Get the mass of the base from the URDF file."""
        return self._base_mass_urdf

    def get_base_inertia_from_urdf(self):
        """Get the inertia of the base from the URDF file."""
        return self._base_inertia_urdf

    def get_leg_mass_from_urdf(self):
        """Get the mass of the legs from the URDF file."""
        return self._leg_masses_urdf

    def get_leg_inertia_from_urdf(self):
        """Get the inertia of the legs from the URDF file."""
        return self._leg_inertia_urdf

    def set_base_mass(self, base_mass):
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
            self._pybullet_client.changeDynamics(
                self.quadruped, chassis_id, mass=chassis_mass)

    def set_leg_mass(self, leg_masses):
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
            raise ValueError("The number of values passed to set_leg_mass are "
                             "different than number of leg links and motors.")
        for leg_id, leg_mass in zip(self._leg_link_ids, leg_masses):
            self._pybullet_client.changeDynamics(
                self.quadruped, leg_id, mass=leg_mass)
        motor_masses = leg_masses[len(self._leg_link_ids):]
        for link_id, motor_mass in zip(self._motor_link_ids, motor_masses):
            self._pybullet_client.changeDynamics(
                self.quadruped, link_id, mass=motor_mass)

    def set_base_inertia(self, base_inertias):
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
            self._pybullet_client.changeDynamics(
                self.quadruped, chassis_id, localInertiaDiagonal=chassis_inertia)

    def set_leg_inertia(self, leg_inertias):
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
            raise ValueError("The number of values passed to set_leg_mass are "
                             "different than number of leg links and motors.")
        for leg_id, leg_inertia in zip(self._leg_link_ids, leg_inertias):
            for inertia_value in leg_inertias:
                if (np.asarray(inertia_value) < 0).any():
                    raise ValueError(
                        "Values in inertia matrix should be non-negative.")
            self._pybullet_client.changeDynamics(
                self.quadruped, leg_id, localInertiaDiagonal=leg_inertia)

        motor_inertias = leg_inertias[len(self._leg_link_ids):]
        for link_id, motor_inertia in zip(self._motor_link_ids, motor_inertias):
            for inertia_value in motor_inertias:
                if (np.asarray(inertia_value) < 0).any():
                    raise ValueError(
                        "Values in inertia matrix should be non-negative.")
            self._pybullet_client.changeDynamics(
                self.quadruped, link_id, localInertiaDiagonal=motor_inertia)

    def set_foot_friction(self, foot_friction):
        """Set the lateral friction of the feet.

        Args:
          foot_friction: The lateral friction coefficient of the foot. This value is
            shared by all four feet.
        """
        for link_id in self._foot_link_ids:
            self._pybullet_client.changeDynamics(
                self.quadruped, link_id, lateralFriction=foot_friction)

    def set_foot_restitution(self, foot_restitution):
        """Set the coefficient of restitution at the feet.

        Args:
          foot_restitution: The coefficient of restitution (bounciness) of the feet.
            This value is shared by all four feet.
        """
        for link_id in self._foot_link_ids:
            self._pybullet_client.changeDynamics(
                self.quadruped, link_id, restitution=foot_restitution)

    def set_joint_friction(self, joint_frictions):
        for knee_joint_id, friction in zip(self._foot_link_ids, joint_frictions):
            self._pybullet_client.setJointMotorControl2(
                bodyIndex=self.quadruped,
                jointIndex=knee_joint_id,
                controlMode=self._pybullet_client.VELOCITY_CONTROL,
                targetVelocity=0,
                force=friction)

    def get_num_knee_joints(self):
        return len(self._foot_link_ids)

    def set_battery_voltage(self, voltage):
        self._motor_model.set_voltage(voltage)

    def set_motor_viscous_damping(self, viscous_damping):
        self._motor_model.set_viscous_damping(viscous_damping)

    def set_motor_gains(self, kp, kd):
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

    def get_motor_gains(self):
        """Get the gains of the motor.

        Returns:
          The proportional gain.
          The derivative gain.
        """
        return self._motor_kps, self._motor_kds

    def get_motor_pos_gains(self):
        """Get the position gains of the motor.

        Returns:
          The proportional gain.
        """
        return self._motor_kps

    def get_motor_vel_gains(self):
        """Get the velocity gains of the motor.

        Returns:
          The derivative gain.
        """
        return self._motor_kds

    def set_motor_strength_ratio(self, ratio):
        """Set the strength of all motors relative to the default value.

        Args:
          ratio: The relative strength. A scalar range from 0.0 to 1.0.
        """
        self._motor_model.set_strength_ratios([ratio] * self.num_motors)

    def set_motor_strength_ratios(self, ratios):
        """Set the strength of each motor relative to the default value.

        Args:
          ratios: The relative strength. A numpy array ranging from 0.0 to 1.0.
        """
        self._motor_model.set_strength_ratios(ratios)

    def set_time_steps(self, action_repeat, simulation_step=0.001):
        """Set the time steps of the control and simulation.

        Args:
          action_repeat: The number of simulation steps that the same action is
            repeated.
          simulation_step: The simulation time step.
        """
        self.time_step = simulation_step
        self._action_repeat = action_repeat

    def _get_motor_names(self):
        return self.name_motor

    def _build_action_filter(self):
        sampling_rate = 1 / (self.time_step * self._action_repeat)
        num_joints = self.get_action_dim()
        a_filter = action_filter.ActionFilterButter(
            sampling_rate=sampling_rate, num_joints=num_joints)
        return a_filter

    def _reset_action_filter(self):
        self._action_filter.reset()
        return

    def _filter(self, action):
        # initialize the filter history, since resetting the filter will fill
        # the history with zeros and this can cause sudden movements at the start
        # of each episode
        if self._state_action_counter == 0:
            default_action = self.get_motor_angles()
            self._action_filter.init_history(default_action)

        filtered_action = self._action_filter.filter(action)
        return filtered_action

    def get_default_init_pos(self):
        return self._init_pos

    def get_default_init_ori(self):
        """Returns the init position of the robot.

        It can be either 1) INIT_ORIENTATION or 2) the previous rotation in yaw.
        """
        return self._init_quat

    def get_default_init_joint_pos(self):
        """Get default initial joint pose."""
        joint_pose = (self._init_motor_angle +
                      self._motor_offset) * self._motor_direction
        return joint_pose

    @property
    def pybullet_client(self):
        return self._pybullet_client

    @property
    def joint_states(self):
        return self._joint_states

    @property
    def action_repeat(self):
        return self._action_repeat

    @property
    def randomizer(self):
        return self._randomizers

    @property
    def chassis_link_ids(self):
        return self._chassis_link_ids

    @property
    def is_safe(self):
        return self._is_safe

    @property
    def last_action(self):
        return self._last_action
