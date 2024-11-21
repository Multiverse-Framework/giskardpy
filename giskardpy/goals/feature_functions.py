from __future__ import division

from typing import Optional, Union

from giskardpy import casadi_wrapper as cas
from giskardpy.data_types.data_types import PrefixName, ColorRGBA
from giskardpy.goals.goal import Goal
from giskardpy.god_map import god_map
from giskardpy.motion_graph.tasks.task import WEIGHT_BELOW_CA


class FeatureFunctionGoal(Goal):
    def __init__(self,
                 tip_link: PrefixName,
                 root_link: PrefixName,
                 controlled_feature: Union[cas.Point3, cas.Vector3],
                 reference_feature: Union[cas.Point3, cas.Vector3],
                 name: Optional[str] = None):
        self.root = root_link
        self.tip = tip_link
        if name is None:
            self.name = f'{self.__class__.__name__}/{self.root}/{self.tip}'
        else:
            self.name = name
        super().__init__(self.name)
        root_reference_feature = god_map.world.transform(self.root, reference_feature)
        tip_controlled_feature = god_map.world.transform(self.tip, controlled_feature)

        root_T_tip = god_map.world.compose_fk_expression(self.root, self.tip)
        if isinstance(controlled_feature, cas.Point3):
            self.root_P_controlled_feature = root_T_tip.dot(tip_controlled_feature)
            god_map.debug_expression_manager.add_debug_expression('root_P_controlled_feature',
                                                                  self.root_P_controlled_feature,
                                                                  color=ColorRGBA(r=1, g=0, b=0, a=1))
        elif isinstance(controlled_feature, cas.Vector3):
            self.root_V_controlled_feature = root_T_tip.dot(cas.Vector3(tip_controlled_feature))
            self.root_V_controlled_feature.vis_frame = controlled_feature.vis_frame
            god_map.debug_expression_manager.add_debug_expression('root_V_controlled_feature',
                                                                  self.root_V_controlled_feature,
                                                                  color=ColorRGBA(r=1, g=0, b=0, a=1))

        if isinstance(reference_feature, cas.Point3):
            self.root_P_reference_feature = root_reference_feature
            god_map.debug_expression_manager.add_debug_expression('root_P_reference_feature',
                                                                  self.root_P_reference_feature,
                                                                  color=ColorRGBA(r=0, g=1, b=0, a=1))
        if isinstance(reference_feature, cas.Vector3):
            self.root_V_reference_feature = cas.Vector3(root_reference_feature)
            self.root_V_reference_feature.vis_frame = controlled_feature.vis_frame
            god_map.debug_expression_manager.add_debug_expression('root_V_reference_feature',
                                                                  self.root_V_reference_feature,
                                                                  color=ColorRGBA(r=0, g=1, b=0, a=1))


class AlignPerpendicular(FeatureFunctionGoal):
    def __init__(self,
                 tip_link: PrefixName,
                 root_link: PrefixName,
                 tip_normal: cas.Vector3,
                 reference_normal: cas.Vector3,
                 name: Optional[str] = None,
                 weight: int = WEIGHT_BELOW_CA,
                 max_vel: float = 0.2,
                 start_condition: cas.Expression = cas.BinaryTrue,
                 pause_condition: cas.Expression = cas.BinaryFalse,
                 end_condition: cas.Expression = cas.BinaryFalse
                 ):
        super().__init__(tip_link=tip_link,
                         root_link=root_link,
                         reference_feature=reference_normal,
                         controlled_feature=tip_normal, name=name)

        expr = cas.dot(self.root_V_reference_feature[:3], self.root_V_controlled_feature[:3])

        task = self.create_and_add_task()
        task.add_equality_constraint(reference_velocity=max_vel,
                                     equality_bound=0 - expr,
                                     weight=weight,
                                     task_expression=expr,
                                     name=f'{self.name}_constraint')
        self.connect_monitors_to_all_tasks(start_condition, pause_condition, end_condition)


class HeightGoal(FeatureFunctionGoal):
    def __init__(self,
                 tip_link: PrefixName,
                 root_link: PrefixName,
                 tip_point: cas.Point3,
                 reference_point: cas.Point3,
                 lower_limit: float,
                 upper_limit: float,
                 name: Optional[str] = None,
                 weight: int = WEIGHT_BELOW_CA,
                 max_vel: float = 0.2,
                 start_condition: cas.Expression = cas.BinaryTrue,
                 pause_condition: cas.Expression = cas.BinaryFalse,
                 end_condition: cas.Expression = cas.BinaryFalse
                 ):
        super().__init__(tip_link=tip_link,
                         root_link=root_link,
                         reference_feature=reference_point,
                         controlled_feature=tip_point,
                         name=name)

        expr = cas.distance_projected_on_vector(self.root_P_controlled_feature, self.root_P_reference_feature,
                                                cas.Vector3([0, 0, 1]))

        task = self.create_and_add_task()
        task.add_inequality_constraint(reference_velocity=max_vel,
                                       upper_error=upper_limit - expr,
                                       lower_error=lower_limit - expr,
                                       weight=weight,
                                       task_expression=expr,
                                       name=f'{self.name}_constraint')
        self.connect_monitors_to_all_tasks(start_condition, pause_condition, end_condition)


class DistanceGoal(FeatureFunctionGoal):
    def __init__(self,
                 tip_link: PrefixName,
                 root_link: PrefixName,
                 tip_point: cas.Point3,
                 reference_point: cas.Point3,
                 lower_limit: float,
                 upper_limit: float,
                 name: Optional[str] = None,
                 weight: int = WEIGHT_BELOW_CA,
                 max_vel: float = 0.2,
                 start_condition: cas.Expression = cas.BinaryTrue,
                 pause_condition: cas.Expression = cas.BinaryFalse,
                 end_condition: cas.Expression = cas.BinaryFalse):
        super().__init__(tip_link=tip_link,
                         root_link=root_link,
                         reference_feature=reference_point,
                         controlled_feature=tip_point,
                         name=name)

        projected_vector = cas.distance_vector_projected_on_plane(self.root_P_controlled_feature,
                                                                  self.root_P_reference_feature,
                                                                  cas.Vector3([0, 0, 1]))
        expr = cas.norm(projected_vector)

        task = self.create_and_add_task()
        task.add_inequality_constraint(reference_velocity=max_vel,
                                       upper_error=upper_limit - expr,
                                       lower_error=lower_limit - expr,
                                       weight=weight,
                                       task_expression=expr,
                                       name=f'{self.name}_constraint')
        # An extra constraint that makes the execution more stable
        task.add_inequality_constraint_vector(reference_velocities=[max_vel] * 3,
                                              lower_errors=[0, 0, 0],
                                              upper_errors=[0, 0, 0],
                                              weights=[weight] * 3,
                                              task_expression=projected_vector[:3],
                                              names=[f'{self.name}_extra1', f'{self.name}_extra2', f'{self.name}_extra3'])
        self.connect_monitors_to_all_tasks(start_condition, pause_condition, end_condition)


class AngleGoal(FeatureFunctionGoal):
    def __init__(self,
                 tip_link: PrefixName,
                 root_link: PrefixName,
                 tip_vector: cas.Vector3,
                 reference_vector: cas.Vector3,
                 lower_angle: float,
                 upper_angle: float,
                 name: Optional[str] = None,
                 weight: int = WEIGHT_BELOW_CA,
                 max_vel: float = 0.2,
                 start_condition: cas.Expression = cas.BinaryTrue,
                 pause_condition: cas.Expression = cas.BinaryFalse,
                 end_condition: cas.Expression = cas.BinaryFalse
                 ):
        super().__init__(tip_link=tip_link,
                         root_link=root_link,
                         reference_feature=reference_vector,
                         controlled_feature=tip_vector,
                         name=name)

        expr = cas.angle_between_vector(self.root_V_reference_feature, self.root_V_controlled_feature)

        task = self.create_and_add_task()
        task.add_inequality_constraint(reference_velocity=max_vel,
                                       upper_error=upper_angle - expr,
                                       lower_error=lower_angle - expr,
                                       weight=weight,
                                       task_expression=expr,
                                       name=f'{self.name}_constraint')
        self.connect_monitors_to_all_tasks(start_condition, pause_condition, end_condition)