import numpy as np

from giskardpy.configs.robot_interface_config import RobotInterfaceConfig
from giskardpy.configs.world_config import WorldWithFixedRobot
from giskardpy.data_types import Derivatives


class Ur5eWorldConfig(WorldWithFixedRobot):
    def __init__(self):
        super().__init__({Derivatives.velocity: 0.2,
                          Derivatives.acceleration: np.inf,
                          Derivatives.jerk: 15})


class Ur5eJointTrajServerMultiverseInterface(RobotInterfaceConfig):
    def setup(self):
        self.sync_joint_state_topic('/world/ur5e/joint_states')
        self.add_follow_joint_trajectory_server(
            namespace='/world/ur5e/joint_trajectory_controller')