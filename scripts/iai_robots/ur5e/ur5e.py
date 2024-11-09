#!/usr/bin/env python
import rospy

from giskardpy.configs.giskard import Giskard
from giskardpy.configs.iai_robots.ur5e import Ur5eWorldConfig, Ur5eJointTrajServerMultiverseInterface

if __name__ == '__main__':
    rospy.init_node('giskard')
    giskard = Giskard(world_config=Ur5eWorldConfig(),
                      robot_interface_config=Ur5eJointTrajServerMultiverseInterface())
    giskard.live()
