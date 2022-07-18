import rospy
from geometry_msgs.msg import TransformStamped
from py_trees import Status
from tf2_msgs.msg import TFMessage

import giskardpy.identifier as identifier
from giskardpy.tree.behaviors.plugin import GiskardBehavior
from giskardpy.utils.tfwrapper import normalize_quaternion_msg


class TFPublisher(GiskardBehavior):
    """
    Published tf for attached and evironment objects.
    """

    @profile
    def __init__(self, name, publish_attached_objects, publish_world_objects, tf_topic):
        super(TFPublisher, self).__init__(name)
        self.original_links = set(self.get_robot().link_names)
        self.tf_pub = rospy.Publisher(tf_topic, TFMessage, queue_size=10)
        self.publish_attached_objects = publish_attached_objects
        self.publish_world_objects = publish_world_objects
        self.map_frame = self.god_map.unsafe_get_data(identifier.map_frame)

    def make_transform(self, parent_frame, child_frame, pose):
        tf = TransformStamped()
        tf.header.frame_id = parent_frame
        tf.header.stamp = rospy.get_rostime()
        tf.child_frame_id = child_frame
        tf.transform.translation.x = pose.position.x
        tf.transform.translation.y = pose.position.y
        tf.transform.translation.z = pose.position.z
        tf.transform.rotation = normalize_quaternion_msg(pose.orientation)
        return tf

    @profile
    def update(self):
        try:
            with self.get_god_map() as god_map:
                tf_msg = TFMessage()
                if self.publish_attached_objects:
                    robot_links = set(self.unsafe_get_robot().link_names)
                    attached_links = robot_links - self.original_links
                    if attached_links:
                        get_fk = self.world.compute_fk_pose
                        for link_name in attached_links:
                            parent_link_name = self.robot.get_parent_link_of_link(link_name)
                            fk = get_fk(parent_link_name, link_name)
                            tf = self.make_transform(fk.header.frame_id, str(link_name), fk.pose)
                            tf_msg.transforms.append(tf)
                if self.publish_world_objects:
                    for group_name, group in self.world.groups.items():
                        if group_name == self.god_map.unsafe_get_data(identifier.robot_group_name):
                            # robot frames will exist for sure
                            continue
                        if len(group.joins) > 0:
                            continue
                        get_fk = self.world.compute_fk_pose
                        fk = get_fk(self.world.root_link_name, group.root_link_name)
                        tf = self.make_transform(fk.header.frame_id, str(group.root_link_name), fk.pose)
                        tf_msg.transforms.append(tf)
                self.tf_pub.publish(tf_msg)

        except KeyError as e:
            pass
        except UnboundLocalError as e:
            pass
        except ValueError as e:
            pass
        return Status.SUCCESS