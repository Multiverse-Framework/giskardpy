import datetime
import os
from collections import OrderedDict, defaultdict
from copy import deepcopy
from time import time
from typing import List, Dict, Tuple, Type, Union, Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from giskardpy import casadi_wrapper as w, identifier
from giskardpy.configs.data_types import SupportedQPSolver
from giskardpy.data_types import JointStates, _JointState
from giskardpy.exceptions import OutOfJointLimitsException, \
    HardConstraintsViolatedException, QPSolverException, InfeasibleException
from giskardpy.god_map import GodMap
from giskardpy.model.world import WorldTree
from giskardpy.my_types import derivative_joint_map, Derivatives
from giskardpy.qp.constraint import DerivativeConstraint, IntegralConstraint
from giskardpy.qp.free_variable import FreeVariable
from giskardpy.qp.next_command import NextCommands
from giskardpy.qp.qp_solver import QPSolver
from giskardpy.utils import logging
from giskardpy.utils.utils import memoize, create_path, suppress_stdout, get_all_classes_in_package


def save_pandas(dfs, names, path):
    folder_name = f'{path}/pandas_{datetime.datetime.now().strftime("%Yy-%mm-%dd--%Hh-%Mm-%Ss")}/'
    create_path(folder_name)
    for df, name in zip(dfs, names):
        csv_string = 'name\n'
        with pd.option_context('display.max_rows', None, 'display.max_columns', None):
            if df.shape[1] > 1:
                for column_name, column in df.T.items():
                    csv_string += column.add_prefix(column_name + '||').to_csv(float_format='%.4f')
            else:
                csv_string += df.to_csv(float_format='%.4f')
        file_name2 = f'{folder_name}{name}.csv'
        with open(file_name2, 'w') as f:
            f.write(csv_string)


class Parent:
    free_variables: List[FreeVariable]
    constraints: List[IntegralConstraint]
    velocity_constraints: List[DerivativeConstraint]

    def __init__(self,
                 free_variables: List[FreeVariable],
                 constraints: List[IntegralConstraint],
                 derivative_constraints: List[DerivativeConstraint],
                 sample_period: float, prediction_horizon: int, order: Derivatives,):
        self.free_variables = free_variables  # type: list[FreeVariable]
        self.constraints = constraints  # type: list[IntegralConstraint]
        self.derivative_constraints = derivative_constraints  # type: list[DerivativeConstraint]
        self.prediction_horizon = prediction_horizon
        self.dt = sample_period
        self.order = order

    def replace_hack(self, expression: Union[float, w.Expression], new_value):
        if not isinstance(expression, w.Expression):
            return expression
        hack = GodMap().to_symbol(identifier.hack)
        expression.s = w.ca.substitute(expression.s, hack.s, new_value)
        return expression

    def get_derivative_constraints(self, derivative: Derivatives) -> List[DerivativeConstraint]:
        return [c for c in self.derivative_constraints if c.derivative == derivative]

    @property
    def velocity_constraints(self) -> List[DerivativeConstraint]:
        return self.get_derivative_constraints(Derivatives.velocity)

    @property
    def acceleration_constraints(self) -> List[DerivativeConstraint]:
        return self.get_derivative_constraints(Derivatives.acceleration)

    @property
    def jerk_constraints(self) -> List[DerivativeConstraint]:
        return self.get_derivative_constraints(Derivatives.jerk)

    def _sorter(self, *args):
        """
        Sorts every arg dict individually and then appends all of them.
        :arg args: a bunch of dicts
        :return: list
        """
        result = []
        result_names = []
        for arg in args:
            result.extend(self.__helper(arg))
            result_names.extend(self.__helper_names(arg))
        return result, result_names

    def __helper(self, param):
        return [x for _, x in sorted(param.items())]

    def __helper_names(self, param):
        return [x for x, _ in sorted(param.items())]


class H(Parent):
    def __init__(self,
                 free_variables: List[FreeVariable],
                 constraints: List[IntegralConstraint],
                 derivative_constraints: List[DerivativeConstraint],
                 sample_period: float,
                 prediction_horizon: int,
                 order: Derivatives,
                 default_limits: bool = False):
        super().__init__(free_variables, constraints, derivative_constraints, sample_period, prediction_horizon, order)
        self.height = 0
        self._compute_height()
        self.evaluated = True

    def _compute_height(self):
        self.height = self.number_of_free_variables_with_horizon()
        self.height += self.number_of_constraint_derivative_variables()
        self.height += self.number_of_constraint_error_variables()

    @property
    def width(self):
        return self.height

    def number_of_free_variables_with_horizon(self):
        h = 0
        for v in self.free_variables:
            h += (min(v.order, self.order)) * self.prediction_horizon
        return h

    def number_of_constraint_derivative_variables(self):
        h = 0
        for d in range(Derivatives.velocity, self.order + 1):
            d = Derivatives(d)
            for c in self.get_derivative_constraints(d):
                h += c.control_horizon
        return h

    def number_of_constraint_error_variables(self):
        return len(self.constraints)

    @profile
    def weights(self):
        params = []
        weights = defaultdict(dict)  # maps order to joints
        for t in range(self.prediction_horizon):
            for v in self.free_variables:  # type: FreeVariable
                for o in Derivatives.range(Derivatives.velocity, min(v.order, self.order)):
                    o = Derivatives(o)
                    weights[o][f't{t:03}/{v.position_name}/{o}'] = v.normalized_weight(t, o,
                                                                                       self.prediction_horizon,
                                                                                       evaluated=self.evaluated)
        for _, weight in sorted(weights.items()):
            params.append(weight)

        for d in Derivatives.range(Derivatives.velocity, self.order):
            derivative_constr_weights = {}
            for t in range(self.prediction_horizon):
                d = Derivatives(d)
                for c in self.get_derivative_constraints(d):  # type: DerivativeConstraint
                    if t < c.control_horizon:
                        derivative_constr_weights[f't{t:03}/{c.name}'] = c.normalized_weight(t)
            params.append(derivative_constr_weights)

        error_slack_weights = {f'{c.name}/error': c.normalized_weight(self.prediction_horizon) for c in
                               self.constraints}

        params.append(error_slack_weights)
        weights, _ = self._sorter(*params)
        for i in range(len(weights)):
            weights[i] = self.replace_hack(weights[i], 0)
        return weights


class B(Parent):

    def __init__(self,
                 free_variables: List[FreeVariable],
                 constraints: List[IntegralConstraint],
                 derivative_constraints: List[DerivativeConstraint],
                 sample_period: float,
                 prediction_horizon: int,
                 order: Derivatives,
                 default_limits: bool = False):
        super().__init__(free_variables, constraints, derivative_constraints, sample_period, prediction_horizon, order)
        self.no_limits = 1e4
        self.evaluated = True
        self.default_limits = default_limits

    def get_derivative_slack_limits(self, derivative: Derivatives) \
            -> Tuple[Dict[str, w.Expression], Dict[str, w.Expression]]:
        lower_slack = {}
        upper_slack = {}
        for t in range(self.prediction_horizon):
            for c in self.get_derivative_constraints(derivative):
                if t < c.control_horizon:
                    lower_slack[f't{t:03}/{c.name}'] = c.lower_slack_limit[t]
                    upper_slack[f't{t:03}/{c.name}'] = c.upper_slack_limit[t]
        return lower_slack, upper_slack

    def get_lower_error_slack_limits(self):
        return {f'{c.name}/error': c.lower_slack_limit for c in self.constraints}

    def get_upper_error_slack_limits(self):
        return {f'{c.name}/error': c.upper_slack_limit for c in self.constraints}

    def __call__(self):
        lb = defaultdict(dict)
        ub = defaultdict(dict)
        for t in range(self.prediction_horizon):
            for v in self.free_variables:  # type: FreeVariable
                for derivative in Derivatives.range(Derivatives.velocity, min(v.order, self.order)):
                    if t == self.prediction_horizon - 1 \
                            and derivative < min(v.order, self.order) \
                            and self.prediction_horizon > 2:  # and False:
                        lb[derivative][f't{t:03}/{v.name}/{derivative}'] = 0
                        ub[derivative][f't{t:03}/{v.name}/{derivative}'] = 0
                    else:
                        lb[derivative][f't{t:03}/{v.name}/{derivative}'] = v.get_lower_limit(derivative, evaluated=self.evaluated)
                        ub[derivative][f't{t:03}/{v.name}/{derivative}'] = v.get_upper_limit(derivative, evaluated=self.evaluated)
        lb_params = []
        ub_params = []
        for derivative, x in sorted(lb.items()):
            lb_params.append(x)
        for derivative, x in sorted(ub.items()):
            ub_params.append(x)

        for d in range(Derivatives.velocity, self.order + 1):
            d = Derivatives(d)
            lower_slack, upper_slack = self.get_derivative_slack_limits(d)
            lb_params.append(lower_slack)
            ub_params.append(upper_slack)

        lb_params.append(self.get_lower_error_slack_limits())
        ub_params.append(self.get_upper_error_slack_limits())

        lb, self.names = self._sorter(*lb_params)
        ub, _ = self._sorter(*ub_params)
        for i in range(len(lb)):
            lb[i] = self.replace_hack(lb[i], 0)
            ub[i] = self.replace_hack(ub[i], 0)
        return lb, ub


class BA(Parent):

    def __init__(self,
                 free_variables: List[FreeVariable],
                 constraints: List[IntegralConstraint],
                 derivative_constraints: List[DerivativeConstraint],
                 sample_period: float,
                 prediction_horizon: int,
                 order: Derivatives,
                 default_limits=False):
        super().__init__(free_variables, constraints, derivative_constraints, sample_period, prediction_horizon, order)
        self.round_to = 5
        self.round_to2 = 10
        self.default_limits = default_limits
        self.evaluated = True

    def get_derivative_Ax_limits(self, derivative: Derivatives) \
            -> Tuple[Dict[str, w.Expression], Dict[str, w.Expression]]:
        lower = {}
        upper = {}
        for t in range(self.prediction_horizon):
            for c in self.get_derivative_constraints(derivative):
                if t < c.control_horizon:
                    lower[f't{t:03}/{c.name}'] = w.limit(c.lower_limit[t] * self.dt,
                                                         -c.normalization_factor * self.dt,
                                                         c.normalization_factor * self.dt)
                    upper[f't{t:03}/{c.name}'] = w.limit(c.upper_limit[t] * self.dt,
                                                         -c.normalization_factor * self.dt,
                                                         c.normalization_factor * self.dt)
        return lower, upper

    @memoize
    def get_lower_constraint_error(self):
        return {f'{c.name}/e': w.limit(c.lower_error,
                                       -c.velocity_limit * self.dt * c.control_horizon,
                                       c.velocity_limit * self.dt * c.control_horizon)
                for c in self.constraints}

    @memoize
    def get_upper_constraint_error(self):
        return {f'{c.name}/e': w.limit(c.upper_error,
                                       -c.velocity_limit * self.dt * c.control_horizon,
                                       c.velocity_limit * self.dt * c.control_horizon)
                for c in self.constraints}

    def __call__(self) -> tuple:
        lb = {}
        ub = {}
        # position limits
        for t in range(self.prediction_horizon):
            for v in self.free_variables:  # type: FreeVariable
                if v.has_position_limits():
                    normal_lower_bound = w.round_up(
                        v.get_lower_limit(Derivatives.position,
                                          False, evaluated=self.evaluated) - v.get_symbol(Derivatives.position),
                        self.round_to2)
                    normal_upper_bound = w.round_down(
                        v.get_upper_limit(Derivatives.position,
                                          False, evaluated=self.evaluated) - v.get_symbol(Derivatives.position),
                        self.round_to2)
                    if self.default_limits:
                        if self.order >= Derivatives.jerk:
                            lower_vel = w.min(v.get_upper_limit(derivative=Derivatives.velocity,
                                                                default=False,
                                                                evaluated=True) * self.dt,
                                              v.get_upper_limit(derivative=Derivatives.jerk,
                                                                default=False,
                                                                evaluated=self.evaluated) * self.dt ** 3)
                            upper_vel = w.max(v.get_lower_limit(derivative=Derivatives.velocity,
                                                                default=False,
                                                                evaluated=True) * self.dt,
                                              v.get_lower_limit(derivative=Derivatives.jerk,
                                                                default=False,
                                                                evaluated=self.evaluated) * self.dt ** 3)
                        else:
                            lower_vel = w.min(v.get_upper_limit(derivative=Derivatives.velocity,
                                                                default=False,
                                                                evaluated=True) * self.dt,
                                              v.get_upper_limit(derivative=Derivatives.acceleration,
                                                                default=False,
                                                                evaluated=self.evaluated) * self.dt ** 2)
                            upper_vel = w.max(v.get_lower_limit(derivative=Derivatives.velocity,
                                                                default=False,
                                                                evaluated=True) * self.dt,
                                              v.get_lower_limit(derivative=Derivatives.acceleration,
                                                                default=False,
                                                                evaluated=self.evaluated) * self.dt ** 2)
                        lower_bound = w.if_greater(normal_lower_bound, 0,
                                                   if_result=lower_vel,
                                                   else_result=normal_lower_bound)
                        lb[f't{t:03d}/{v.name}/p_limit'] = lower_bound

                        upper_bound = w.if_less(normal_upper_bound, 0,
                                                if_result=upper_vel,
                                                else_result=normal_upper_bound)
                        ub[f't{t:03d}/{v.name}/p_limit'] = upper_bound
                    else:
                        lb[f't{t:03d}/{v.name}/p_limit'] = normal_lower_bound
                        ub[f't{t:03d}/{v.name}/p_limit'] = normal_upper_bound

        l_last_stuff = defaultdict(dict)
        u_last_stuff = defaultdict(dict)
        for v in self.free_variables:
            for o in Derivatives.range(Derivatives.velocity, min(v.order, self.order)):
                l_last_stuff[o][f'{v.name}/last_{o}'] = w.round_down(v.get_symbol(o), self.round_to)
                u_last_stuff[o][f'{v.name}/last_{o}'] = w.round_up(v.get_symbol(o), self.round_to)

        derivative_link = defaultdict(dict)
        for t in range(self.prediction_horizon - 1):
            for v in self.free_variables:
                for o in range(1, min(v.order, self.order)):
                    derivative_link[o][f't{t:03}/{o}/{v.name}/link'] = 0

        lb_params = [lb]
        ub_params = [ub]
        for o in range(1, self.order):
            lb_params.append(l_last_stuff[o])
            lb_params.append(derivative_link[o])
            ub_params.append(u_last_stuff[o])
            ub_params.append(derivative_link[o])

        for d in range(Derivatives.velocity, self.order + 1):
            d = Derivatives(d)
            lower, upper = self.get_derivative_Ax_limits(d)
            lb_params.append(lower)
            ub_params.append(upper)

        lb_params.append(self.get_lower_constraint_error())
        ub_params.append(self.get_upper_constraint_error())

        lbA, self.names = self._sorter(*lb_params)
        ubA, _ = self._sorter(*ub_params)

        for i in range(len(lbA)):
            lbA[i] = self.replace_hack(lbA[i], 0)
            ubA[i] = self.replace_hack(ubA[i], 0)
        return lbA, ubA


class A(Parent):
    def __init__(self,
                 free_variables: List[FreeVariable],
                 constraints: List[IntegralConstraint],
                 derivative_constraints: List[DerivativeConstraint],
                 sample_period: float,
                 prediction_horizon: int,
                 order: Derivatives,

                 default_limits: bool = False):
        super().__init__(free_variables, constraints, derivative_constraints,
                         sample_period, prediction_horizon, order)
        self.joints = {}
        self.height = 0
        self._compute_height()
        self.width = 0
        self._compute_width()
        self.default_limits = default_limits

    def _compute_height(self):
        # rows for position limits of non-continuous joints
        self.height = self.prediction_horizon * (self.num_position_limits())
        # rows for linking vel/acc/jerk
        self.height += self.number_of_joints * self.prediction_horizon * (self.order - 1)
        # rows for velocity constraints
        for i, c in enumerate(self.velocity_constraints):
            self.height += c.control_horizon
        # rows for acceleration constraints
        for i, c in enumerate(self.acceleration_constraints):
            self.height += c.control_horizon
        # rows for jerk constraints
        for i, c in enumerate(self.jerk_constraints):
            self.height += c.control_horizon
        # row for constraint error
        self.height += len(self.constraints)

    def _compute_width(self):
        # columns for joint vel/acc/jerk symbols
        self.width = self.number_of_joints * self.prediction_horizon * self.order
        # columns for velocity constraints
        for i, c in enumerate(self.velocity_constraints):
            self.width += c.control_horizon
        # columns for acceleration constraints
        for i, c in enumerate(self.acceleration_constraints):
            self.width += c.control_horizon
        # columns for jerk constraints
        for i, c in enumerate(self.jerk_constraints):
            self.width += c.control_horizon
        # slack variable for constraint error
        self.width += len(self.constraints)
        # constraints for getting out of hard limits
        # if self.default_limits:
        #     self.width += self.num_position_limits()

    @property
    def number_of_joints(self):
        return len(self.free_variables)

    @memoize
    def num_position_limits(self):
        return self.number_of_joints - self.num_of_continuous_joints()

    @memoize
    def num_of_continuous_joints(self):
        return len([v for v in self.free_variables if not v.has_position_limits()])

    def get_constraint_expressions(self):
        return self._sorter({c.name: c.expression for c in self.constraints})[0]

    def get_derivative_constraint_expressions(self, derivative: Derivatives):
        return self._sorter({c.name: c.expression for c in self.derivative_constraints if c.derivative == derivative})[
            0]

    def get_free_variable_symbols(self, order: Derivatives):
        return self._sorter({v.position_name: v.get_symbol(order) for v in self.free_variables})[0]

    @profile
    def construct_A(self):
        #         |   t1   |   tn   |   t1   |   tn   |   t1   |   tn   |   t1   |   tn   |
        #         |v1 v2 vn|v1 v2 vn|a1 a2 an|a1 a2 an|j1 j2 jn|j1 j2 jn|s1 s2 sn|s1 s2 sn|
        #         |-----------------------------------------------------------------------|
        #         |sp      |        |        |        |        |        |        |        |
        #         |   sp   |        |        |        |        |        |        |        |
        #         |      sp|        |        |        |        |        |        |        |
        #         |-----------------------------------------------------------------------|
        #         |sp      |sp      |        |        |        |        |        |        |
        #         |   sp   |   sp   |        |        |        |        |        |        |
        #         |      sp|      sp|        |        |        |        |        |        |
        #         |=======================================================================|
        #         | 1      |        |-sp     |        |        |        |        |        |
        #         |    1   |        |   -sp  |        |        |        |        |        |
        #         |       1|        |     -sp|        |        |        |        |        |
        #         |-----------------------------------------------------------------------|
        #         |-1      | 1      |        |-sp     |        |        |        |        |
        #         |   -1   |    1   |        |   -sp  |        |        |        |        |
        #         |      -1|       1|        |     -sp|        |        |        |        |
        #         |=======================================================================|
        #         |        |        | 1      |        |-sp     |        |-sp     |        |
        #         |        |        |    1   |        |   -sp  |        |   -sp  |        |
        #         |        |        |       1|        |     -sp|        |     -sp|        |
        #         |-----------------------------------------------------------------------|
        #         |        |        |-1      | 1      |        |-sp     |        |-sp     |
        #         |        |        |   -1   |    1   |        |   -sp  |        |   -sp  |
        #         |        |        |      -1|       1|        |     -sp|        |     -sp|
        #         |=======================================================================|
        #         |  J*sp  |        |        |        |        |        |   sp   |        |
        #         |-----------------------------------------------------------------------|
        #         |        |  J*sp  |        |        |        |        |        |   sp   |
        #         |-----------------------------------------------------------------------|
        #         |  J*sp  |  J*sp  |        |        |        |        | sp*ph  | sp*ph  |
        #         |-----------------------------------------------------------------------|

        #         |   t1   |   t2   |   t3   |   t3   |
        #         |v1 v2 vn|v1 v2 vn|v1 v2 vn|v1 v2 vn|
        #         |-----------------------------------|
        #         |sp      |        |        |        |
        #         |   sp   |        |        |        |
        #         |      sp|        |        |        |
        #         |sp      |sp      |        |        |
        #         |   sp   |   sp   |        |        |
        #         |      sp|      sp|        |        |
        #         |sp      |sp      |sp      |        |
        #         |   sp   |   sp   |   sp   |        |
        #         |      sp|      sp|      sp|        |
        #         |sp      |sp      |sp      |sp      |
        #         |   sp   |   sp   |   sp   |   sp   |
        #         |      sp|      sp|      sp|      sp|
        #         |===================================|
        number_of_joints = self.number_of_joints

        num_position_constraints = self.prediction_horizon * number_of_joints
        num_derivative_links = number_of_joints * self.prediction_horizon * (self.order - 1)
        number_of_vel_rows = len(self.velocity_constraints) * self.prediction_horizon
        number_of_acc_rows = len(self.acceleration_constraints) * self.prediction_horizon
        number_of_jerk_rows = len(self.jerk_constraints) * self.prediction_horizon
        number_of_task_constr_rows = len(self.constraints)

        number_of_non_slack_columns = number_of_joints * self.prediction_horizon * (self.order)
        number_of_vel_slack_columns = len(self.velocity_constraints) * self.prediction_horizon
        number_of_acc_slack_columns = len(self.acceleration_constraints) * self.prediction_horizon
        number_of_jerk_slack_columns = len(self.jerk_constraints) * self.prediction_horizon
        number_of_integral_slack_columns = len(self.constraints)
        A_soft = w.zeros(
            num_position_constraints + num_derivative_links
            + number_of_vel_rows + number_of_acc_rows + number_of_jerk_rows + number_of_task_constr_rows,
            number_of_non_slack_columns +
            number_of_vel_slack_columns + number_of_acc_slack_columns + number_of_jerk_slack_columns
            + number_of_integral_slack_columns
        )

        rows_to_delete = []
        columns_to_delete = []

        # position limits -----------------------------------------
        vertical_offset = num_position_constraints
        for p in range(1, self.prediction_horizon + 1):
            matrix_size = number_of_joints * p
            I = w.eye(matrix_size) * self.dt
            start = vertical_offset - matrix_size
            A_soft[start:vertical_offset, :matrix_size] += I

        # delete rows with position limits of continuous joints
        continuous_joint_indices = [i for i, v in enumerate(self.free_variables) if not v.has_position_limits()]
        for o in range(self.prediction_horizon):
            for i in continuous_joint_indices:
                rows_to_delete.append(i + len(self.free_variables) * o)
        # position limits -----------------------------------------

        # derivative links ----------------------------------------
        I = w.eye(num_derivative_links)
        A_soft[vertical_offset:vertical_offset + num_derivative_links, :num_derivative_links] += I
        h_offset = number_of_joints * self.prediction_horizon
        A_soft[vertical_offset:vertical_offset + num_derivative_links,
        h_offset:h_offset + num_derivative_links] += -I * self.dt

        I_height = number_of_joints * (self.prediction_horizon - 1)
        I = -w.eye(I_height)
        offset_v = vertical_offset
        offset_h = 0
        for o in range(self.order - 1):
            offset_v += number_of_joints
            A_soft[offset_v:offset_v + I_height, offset_h:offset_h + I_height] += I
            offset_v += I_height
            offset_h += self.prediction_horizon * number_of_joints
        # vertical_offset = vertical_offset + num_derivative_links
        # derivative links ----------------------------------------

        # velocity constraints ------------------------------------
        expressions = w.Expression(self.get_derivative_constraint_expressions(Derivatives.velocity))
        if len(expressions) > 0:
            vertical_offset = num_position_constraints + num_derivative_links
            next_vertical_offset = num_position_constraints + num_derivative_links + number_of_vel_rows
            for order in range(self.order):
                order = Derivatives(order)
                J_vel = w.jacobian(expressions=expressions,
                                   symbols=self.get_free_variable_symbols(order)) * self.dt
                J_vel_limit_block = w.kron(w.eye(self.prediction_horizon), J_vel)
                horizontal_offset = self.number_of_joints * self.prediction_horizon
                A_soft[vertical_offset:next_vertical_offset,
                horizontal_offset * order:horizontal_offset * (order + 1)] = J_vel_limit_block
            # velocity constraint slack
            I = w.eye(number_of_vel_rows) * self.dt
            A_soft[vertical_offset:next_vertical_offset,
            number_of_non_slack_columns:number_of_non_slack_columns + number_of_vel_slack_columns] = I
            # delete rows if control horizon of constraint shorter than prediction horizon
            # delete columns where control horizon is shorter than prediction horizon
            for t in range(self.prediction_horizon):
                for i, c in enumerate(self.velocity_constraints):
                    h_index = number_of_non_slack_columns + i + (t * len(self.velocity_constraints))
                    v_index = vertical_offset + i + (t * len(self.velocity_constraints))
                    if t + 1 > c.control_horizon:
                        rows_to_delete.append(v_index)
                        columns_to_delete.append(h_index)
        # velocity constraints ------------------------------------

        # acceleration constraints --------------------------------
        v_acc_start = num_position_constraints + num_derivative_links + number_of_vel_rows
        v_acc_end = v_acc_start + number_of_acc_rows
        h_acc_start = number_of_non_slack_columns + number_of_vel_slack_columns
        h_acc_end = h_acc_start + number_of_acc_slack_columns
        expressions = w.Expression(self.get_derivative_constraint_expressions(Derivatives.acceleration))
        if len(expressions) > 0:
            assert self.order >= Derivatives.jerk
            # task acceleration = Jd_q * qd + (J_q + Jd_qd) * qdd + J_qd * qddd
            J_q = w.jacobian(expressions=expressions,
                             symbols=self.get_free_variable_symbols(Derivatives.position)) * self.dt
            Jd_q = w.jacobian_dot(expressions=expressions,
                                  symbols=self.get_free_variable_symbols(Derivatives.position),
                                  symbols_dot=self.get_free_variable_symbols(Derivatives.velocity)) * self.dt
            J_qd = w.jacobian(expressions=expressions,
                              symbols=self.get_free_variable_symbols(Derivatives.velocity)) * self.dt
            Jd_qd = w.jacobian_dot(expressions=expressions,
                                   symbols=self.get_free_variable_symbols(Derivatives.velocity),
                                   symbols_dot=self.get_free_variable_symbols(
                                       Derivatives.acceleration)) * self.dt
            J_vel_block = w.kron(w.eye(self.prediction_horizon), Jd_q)
            J_acc_block = w.kron(w.eye(self.prediction_horizon), J_q + Jd_qd)
            J_jerk_block = w.kron(w.eye(self.prediction_horizon), J_qd)
            horizontal_offset = self.number_of_joints * self.prediction_horizon
            A_soft[v_acc_start:v_acc_end, :horizontal_offset] = J_vel_block
            A_soft[v_acc_start:v_acc_end, horizontal_offset:horizontal_offset * 2] = J_acc_block
            A_soft[v_acc_start:v_acc_end, horizontal_offset * 2:horizontal_offset * 3] = J_jerk_block
            # velocity constraint slack
            I = w.eye(J_vel_block.shape[0]) * self.dt
            A_soft[v_acc_start:v_acc_end, h_acc_start:h_acc_end] = I
            # delete rows if control horizon of constraint shorter than prediction horizon
            # delete columns where control horizon is shorter than prediction horizon
            for t in range(self.prediction_horizon):
                for i, c in enumerate(self.acceleration_constraints):
                    h_index = h_acc_start + i + (t * len(self.acceleration_constraints))
                    v_index = v_acc_start + i + (t * len(self.acceleration_constraints))
                    if t + 1 > c.control_horizon:
                        rows_to_delete.append(v_index)
                        columns_to_delete.append(h_index)
        # acceleration constraints --------------------------------

        # jerk constraints --------------------------------
        v_jerk_start = num_position_constraints + num_derivative_links + number_of_vel_rows + number_of_acc_rows
        v_jerk_end = v_jerk_start + number_of_jerk_rows
        h_jerk_start = number_of_non_slack_columns + number_of_vel_slack_columns + number_of_acc_slack_columns
        h_jerk_end = h_jerk_start + number_of_jerk_slack_columns
        expressions = w.Expression(self.get_derivative_constraint_expressions(Derivatives.jerk))
        if len(expressions) > 0:
            assert self.order >= Derivatives.snap
            # task acceleration = Jd_q * qd + (J_q + Jd_qd) * qdd + J_qd * qddd
            J_q = self.dt * w.jacobian(expressions=expressions,
                                       symbols=self.get_free_variable_symbols(Derivatives.position))
            Jd_q = self.dt * w.jacobian_dot(expressions=expressions,
                                            symbols=self.get_free_variable_symbols(Derivatives.position),
                                            symbols_dot=self.get_free_variable_symbols(Derivatives.velocity))
            Jdd_q = self.dt * w.jacobian_ddot(expressions=expressions,
                                              symbols=self.get_free_variable_symbols(Derivatives.position),
                                              symbols_dot=self.get_free_variable_symbols(Derivatives.velocity),
                                              symbols_ddot=self.get_free_variable_symbols(Derivatives.acceleration))
            J_qd = self.dt * w.jacobian(expressions=expressions,
                                        symbols=self.get_free_variable_symbols(Derivatives.velocity))
            Jd_qd = self.dt * w.jacobian_dot(expressions=expressions,
                                             symbols=self.get_free_variable_symbols(Derivatives.velocity),
                                             symbols_dot=self.get_free_variable_symbols(Derivatives.acceleration))
            Jdd_qd = self.dt * w.jacobian_ddot(expressions=expressions,
                                               symbols=self.get_free_variable_symbols(Derivatives.velocity),
                                               symbols_dot=self.get_free_variable_symbols(Derivatives.acceleration),
                                               symbols_ddot=self.get_free_variable_symbols(Derivatives.jerk))
            J_vel_block = w.kron(w.eye(self.prediction_horizon), Jdd_q)
            J_acc_block = w.kron(w.eye(self.prediction_horizon), 2 * Jd_q + Jdd_qd)
            J_jerk_block = w.kron(w.eye(self.prediction_horizon), J_q + 2 * Jd_qd)
            J_snap_block = w.kron(w.eye(self.prediction_horizon), J_qd)
            horizontal_offset = self.number_of_joints * self.prediction_horizon
            A_soft[v_jerk_start:v_jerk_end, :horizontal_offset] = J_vel_block
            A_soft[v_jerk_start:v_jerk_end, horizontal_offset:horizontal_offset * 2] = J_acc_block
            A_soft[v_jerk_start:v_jerk_end, horizontal_offset * 2:horizontal_offset * 3] = J_jerk_block
            A_soft[v_jerk_start:v_jerk_end, horizontal_offset * 3:horizontal_offset * 4] = J_snap_block
            # slack
            I = w.eye(J_vel_block.shape[0]) * self.dt
            A_soft[v_jerk_start:v_jerk_end, h_jerk_start:h_jerk_end] = I
            # delete rows if control horizon of constraint shorter than prediction horizon
            # delete columns where control horizon is shorter than prediction horizon
            for t in range(self.prediction_horizon):
                for i, c in enumerate(self.jerk_constraints):
                    h_index = h_jerk_start + i + (t * len(self.jerk_constraints))
                    v_index = v_jerk_start + i + (t * len(self.jerk_constraints))
                    if t + 1 > c.control_horizon:
                        rows_to_delete.append(v_index)
                        columns_to_delete.append(h_index)
        # jerk constraints --------------------------------

        # J stack for total error
        if len(self.constraints) > 0:
            vertical_offset = num_position_constraints + num_derivative_links \
                              + number_of_vel_rows + number_of_acc_rows + number_of_jerk_rows
            next_vertical_offset = vertical_offset + number_of_task_constr_rows
            for order in range(self.order):
                order = Derivatives(order)
                J_err = w.jacobian(expressions=w.Expression(self.get_constraint_expressions()),
                                   symbols=self.get_free_variable_symbols(order)) * self.dt
                J_hstack = w.hstack([J_err for _ in range(self.prediction_horizon)])
                # set jacobian entry to 0 if control horizon shorter than prediction horizon
                for i, c in enumerate(self.constraints):
                    # offset = vertical_offset + i
                    J_hstack[i, c.control_horizon * len(self.free_variables):] = 0
                horizontal_offset = J_hstack.shape[1]
                A_soft[vertical_offset:next_vertical_offset,
                horizontal_offset * (order):horizontal_offset * (order + 1)] = J_hstack

            # extra slack variable for total error
            I = w.diag(w.Expression([self.dt * c.control_horizon for c in self.constraints]))
            A_soft[vertical_offset:next_vertical_offset, -I.shape[1]:] = I

        A_soft.remove(rows_to_delete, [])
        A_soft.remove([], columns_to_delete)

        A_soft = self.replace_hack(A_soft, 1)

        return A_soft

    def A(self):
        return self.construct_A()


available_solvers: Dict[SupportedQPSolver, Type[QPSolver]] = {}


def detect_solvers():
    global available_solvers
    solver_name: str
    qp_solver_class: Type[QPSolver]
    for solver_name, qp_solver_class in get_all_classes_in_package('giskardpy.qp', QPSolver, silent=True).items():
        try:
            available_solvers[qp_solver_class.solver_id] = qp_solver_class
        except Exception:
            pass
    solver_names = [str(solver_name).split('.')[1] for solver_name in available_solvers.keys()]
    logging.loginfo(f'Found these qp solvers: {solver_names}')


detect_solvers()


class QPController:
    """
    Wraps around QP Solver. Builds the required matrices from constraints.
    """
    debug_expressions: Dict[str, w.all_expressions]
    compiled_debug_expressions: Dict[str, w.CompiledFunction]
    evaluated_debug_expressions: Dict[str, np.ndarray]

    def __init__(self,
                 sample_period: float,
                 prediction_horizon: int,
                 solver_id: Optional[SupportedQPSolver] = None,
                 free_variables: List[FreeVariable] = None,
                 constraints: List[IntegralConstraint] = None,
                 velocity_constraints: List[DerivativeConstraint] = None,
                 debug_expressions: Dict[str, Union[w.Symbol, float]] = None,
                 retries_with_relaxed_constraints: int = 0,
                 retry_added_slack: float = 100,
                 retry_weight_factor: float = 100):
        self.free_variables = []
        self.constraints = []
        self.velocity_constraints = []
        self.debug_expressions = {}
        self.prediction_horizon = prediction_horizon
        self.sample_period = sample_period
        self.retries_with_relaxed_constraints = retries_with_relaxed_constraints
        self.retry_added_slack = retry_added_slack
        self.retry_weight_factor = retry_weight_factor
        self.evaluated_debug_expressions = {}
        self.xdot_full = None
        if free_variables is not None:
            self.add_free_variables(free_variables)
        if constraints is not None:
            self.add_constraints(constraints)
        if velocity_constraints is not None:
            self.add_velocity_constraints(velocity_constraints)
        if debug_expressions is not None:
            self.add_debug_expressions(debug_expressions)

        if solver_id is not None:
            qp_solver_class = available_solvers[solver_id]
        else:
            for solver_id in SupportedQPSolver:
                if solver_id in available_solvers:
                    qp_solver_class = available_solvers[solver_id]
                    break
            else:
                raise QPSolverException(f'No qp solver found')
        num_non_slack = len(self.free_variables) * self.prediction_horizon * (self.order)
        self.qp_solver = qp_solver_class(num_non_slack=num_non_slack,
                                         retry_added_slack=self.retry_added_slack,
                                         retry_weight_factor=self.retry_weight_factor,
                                         retries_with_relaxed_constraints=self.retries_with_relaxed_constraints)
        logging.loginfo(f'Using QP Solver \'{solver_id}\'')
        logging.loginfo(f'Prediction horizon: \'{self.prediction_horizon}\'')

    def add_free_variables(self, free_variables):
        """
        :type free_variables: list
        """
        if len(free_variables) == 0:
            raise QPSolverException('Cannot solve qp with no free variables')
        self.free_variables.extend(list(sorted(free_variables, key=lambda x: x.position_name)))
        l = [x.position_name for x in free_variables]
        duplicates = set([x for x in l if l.count(x) > 1])
        self.order = Derivatives(min(self.prediction_horizon + 1, max(v.order for v in self.free_variables)))
        assert duplicates == set(), f'there are free variables with the same name: {duplicates}'

    def get_free_variable(self, name):
        """
        :type name: str
        :rtype: FreeVariable
        """
        for v in self.free_variables:
            if v.position_name == name:
                return v
        raise KeyError(f'No free variable with name: {name}')

    def add_constraints(self, constraints):
        """
        :type constraints: list
        """
        self.constraints.extend(list(sorted(constraints, key=lambda x: x.name)))
        l = [x.name for x in constraints]
        duplicates = set([x for x in l if l.count(x) > 1])
        assert duplicates == set(), f'there are multiple constraints with the same name: {duplicates}'
        for c in self.constraints:
            c.control_horizon = min(c.control_horizon, self.prediction_horizon)
            self.check_control_horizon(c)

    def add_velocity_constraints(self, constraints):
        """
        :type constraints: list
        """
        self.velocity_constraints.extend(list(sorted(constraints, key=lambda x: x.name)))
        l = [x.name for x in constraints]
        duplicates = set([x for x in l if l.count(x) > 1])
        assert duplicates == set(), f'there are multiple constraints with the same name: {duplicates}'
        for c in self.velocity_constraints:
            self.check_control_horizon(c)

    def check_control_horizon(self, constraint):
        if constraint.control_horizon is None:
            constraint.control_horizon = self.prediction_horizon
        elif constraint.control_horizon <= 0 or not isinstance(constraint.control_horizon, int):
            raise ValueError(f'Control horizon of {constraint.name} is {constraint.control_horizon}, '
                             f'it has to be an integer 1 <= control horizon <= prediction horizon')
        elif constraint.control_horizon > self.prediction_horizon:
            logging.logwarn(f'Specified control horizon of {constraint.name} is bigger than prediction horizon.'
                            f'Reducing control horizon of {constraint.control_horizon} '
                            f'to prediction horizon of {self.prediction_horizon}')
            constraint.control_horizon = self.prediction_horizon

    def add_debug_expressions(self, debug_expressions):
        """
        :type debug_expressions: dict
        """
        self.debug_expressions.update(debug_expressions)

    @profile
    def compile(self):
        self._construct_big_ass_M(default_limits=False)
        self._compile_big_ass_M()
        self._compile_debug_expressions()

    def get_parameter_names(self):
        return self.compiled_big_ass_M.str_params

    @profile
    def _compile_big_ass_M(self):
        t = time()
        free_symbols = w.free_symbols(self.big_ass_M)
        # free_symbols = set(free_symbols)
        # free_symbols = list(free_symbols)
        self.compiled_big_ass_M = self.big_ass_M.compile(free_symbols)
        compilation_time = time() - t
        logging.loginfo(f'Compiled symbolic controller in {compilation_time:.5f}s')

    def _compile_debug_expressions(self):
        t = time()
        self.compiled_debug_expressions = {}
        free_symbols = set()
        for name, expr in self.debug_expressions.items():
            free_symbols.update(expr.free_symbols())
        free_symbols = list(free_symbols)
        for name, expr in self.debug_expressions.items():
            self.compiled_debug_expressions[name] = expr.compile(free_symbols)
        compilation_time = time() - t
        logging.loginfo(f'Compiled debug expressions in {compilation_time:.5f}s')

    def _are_joint_limits_violated(self, percentage: float = 0.0):
        joint_with_position_limits = [x for x in self.free_variables if x.has_position_limits()]
        num_joint_with_position_limits = len(joint_with_position_limits)
        name_replacements = {}
        for old_name in self.p_lbA_raw.index:
            for free_variable in self.free_variables:
                short_old_name = old_name.split('/')[1]
                if short_old_name == free_variable.position_name:
                    name_replacements[old_name] = str(free_variable.name)
        lbA = self.p_lbA_raw[:num_joint_with_position_limits]
        ubA = self.p_ubA_raw[:num_joint_with_position_limits]
        lbA = lbA.rename(name_replacements)
        ubA = ubA.rename(name_replacements)
        joint_range = ubA - lbA
        joint_range *= percentage
        lbA_danger = lbA[lbA > -joint_range].dropna()
        ubA_danger = ubA[ubA < joint_range].dropna()
        msg = None
        if len(lbA_danger) > 0:
            msg = f'The following joints are below their lower position limits by:\n{(-lbA_danger).to_string()}\n'
        if len(ubA_danger) > 0:
            if msg is None:
                msg = ''
            msg += f'The following joints are above their upper position limits by:\n{(-ubA_danger).to_string()}\n'
        return msg

    def save_all_pandas(self):
        if hasattr(self, 'p_xdot') and self.p_xdot is not None:
            save_pandas(
                [self.p_weights, self.p_A, self.p_Ax, self.p_lbA, self.p_ubA, self.p_lb, self.p_ub, self.p_debug,
                 self.p_xdot],
                ['weights', 'A', 'Ax', 'lbA', 'ubA', 'lb', 'ub', 'debug', 'xdot'],
                self.god_map.get_data(identifier.tmp_folder))
        else:
            save_pandas(
                [self.p_weights, self.p_A, self.p_lbA, self.p_ubA, self.p_lb, self.p_ub, self.p_debug],
                ['weights', 'A', 'lbA', 'ubA', 'lb', 'ub', 'debug'],
                self.god_map.get_data(identifier.tmp_folder))

    def _is_inf_in_data(self):
        logging.logerr(f'The following weight entries contain inf:\n'
                       f'{self.p_weights[self.p_weights == np.inf].dropna()}')
        logging.logerr(f'The following lbA entries contain inf:\n'
                       f'{self.p_lbA[self.p_lbA == np.inf].dropna()}')
        logging.logerr(f'The following ubA entries contain inf:\n'
                       f'{self.p_ubA[self.p_ubA == np.inf].dropna()}')
        logging.logerr(f'The following lb entries contain inf:\n'
                       f'{self.p_lb[self.p_lb == np.inf].dropna()}')
        logging.logerr(f'The following ub entries contain inf:\n'
                       f'{self.p_ub[self.p_ub == np.inf].dropna()}')
        if np.inf in self.np_A:
            rows = self.p_A[self.p_A == np.inf].dropna(how='all').dropna(axis=1)
            logging.logerr(f'A contains inf in:\n'
                           f'{list(rows.index)}')
        if np.any(np.isnan(self.np_A)):
            rows = self.p_A.isna()[self.p_A.isna()].dropna(how='all').dropna(axis=1)
            logging.logerr(f'A constrains nan in: \n'
                           f'{list(rows.index)}')
        return True

    @property
    def god_map(self) -> GodMap:
        return GodMap()

    @property
    def world(self) -> WorldTree:
        return self.god_map.get_data(identifier.world)

    def __print_pandas_array(self, array):
        import pandas as pd
        if len(array) > 0:
            with pd.option_context('display.max_rows', None, 'display.max_columns', None):
                print(array)

    def _init_big_ass_M(self):
        self.big_ass_M = w.zeros(self.A.height + 3,
                                 self.A.width + 2)
        # self.debug_v = w.zeros(len(self.debug_expressions), 1)

    def _set_A_soft(self, A_soft):
        self.big_ass_M[:self.A.height, :self.A.width] = A_soft

    def _set_weights(self, weights):
        self.big_ass_M[self.A.height, :-2] = weights

    def _set_lb(self, lb):
        self.big_ass_M[self.A.height + 1, :-2] = lb

    def _set_ub(self, ub):
        self.big_ass_M[self.A.height + 2, :-2] = ub

    def _set_lbA(self, lbA):
        self.big_ass_M[:self.A.height, self.A.width] = lbA

    def _set_ubA(self, ubA):
        self.big_ass_M[:self.A.height, self.A.width + 1] = ubA

    @profile
    def _construct_big_ass_M(self, default_limits=False):
        self.b = B(free_variables=self.free_variables,
                   constraints=self.constraints,
                   derivative_constraints=self.velocity_constraints,
                   sample_period=self.sample_period,
                   prediction_horizon=self.prediction_horizon,
                   order=self.order,
                   default_limits=default_limits)
        self.H = H(free_variables=self.free_variables,
                   constraints=self.constraints,
                   derivative_constraints=self.velocity_constraints,
                   sample_period=self.sample_period,
                   prediction_horizon=self.prediction_horizon,
                   order=self.order,
                   default_limits=default_limits)
        self.bA = BA(free_variables=self.free_variables,
                     constraints=self.constraints,
                     derivative_constraints=self.velocity_constraints,
                     sample_period=self.sample_period,
                     prediction_horizon=self.prediction_horizon,
                     order=self.order,
                     default_limits=default_limits)
        self.A = A(free_variables=self.free_variables,
                   constraints=self.constraints,
                   derivative_constraints=self.velocity_constraints,
                   sample_period=self.sample_period,
                   prediction_horizon=self.prediction_horizon,
                   order=self.order,
                   default_limits=default_limits)

        logging.loginfo(f'Constructing new controller with {self.A.height} constraints '
                        f'and {self.A.width} free variables...')
        self._init_big_ass_M()

        self._set_weights(w.Expression(self.H.weights()))
        self._set_A_soft(self.A.A())
        lbA, ubA = self.bA()
        self._set_lbA(w.Expression(lbA))
        self._set_ubA(w.Expression(ubA))
        lb, ub = self.b()
        self._set_lb(w.Expression(lb))
        self._set_ub(w.Expression(ub))
        self.np_g = np.zeros(self.H.width)
        # self.debug_names = list(sorted(self.debug_expressions.keys()))
        # self.debug_v = w.Expression([self.debug_expressions[name] for name in self.debug_names])

    @profile
    def eval_debug_exprs(self):
        self.evaluated_debug_expressions = {}
        for name, f in self.compiled_debug_expressions.items():
            params = self.god_map.get_values(f.str_params)
            self.evaluated_debug_expressions[name] = f.call2(params).copy()
        return self.evaluated_debug_expressions

    @profile
    def update_filters(self):
        b_filter = self.np_weights != 0
        b_filter[:self.H.number_of_free_variables_with_horizon()] = True
        # offset = self.H.number_of_free_variables_with_horizon() + self.H.number_of_constraint_vel_variables()
        # map_ = self.H.make_error_id_to_vel_ids_map()
        # for i in range(self.H.number_of_contraint_error_variables()):
        #     index = i+offset
        #     if not b_filter[index]:
        #         b_filter[map_[index]] = False

        bA_filter = np.ones(self.A.height, dtype=bool)
        ll = self.H.number_of_constraint_derivative_variables() + self.H.number_of_constraint_error_variables()
        bA_filter[-ll:] = b_filter[-ll:]
        self.b_filter = np.array(b_filter)
        self.bA_filter = np.array(bA_filter)

    def __swap_compiled_matrices(self):
        if not hasattr(self, 'compiled_big_ass_M_with_default_limits'):
            with suppress_stdout():
                self.compiled_big_ass_M_with_default_limits = self.compiled_big_ass_M
                self._construct_big_ass_M(default_limits=True)
                self._compile_big_ass_M()
        else:
            self.compiled_big_ass_M, \
            self.compiled_big_ass_M_with_default_limits = self.compiled_big_ass_M_with_default_limits, \
                                                          self.compiled_big_ass_M

    @property
    def traj_time_in_sec(self):
        return self.god_map.unsafe_get_data(identifier.time) * self.god_map.unsafe_get_data(identifier.sample_period)

    @profile
    def get_cmd(self, substitutions: list) -> NextCommands:
        """
        Uses substitutions for each symbol to compute the next commands for each joint.
        :param substitutions:
        :return: joint name -> joint command
        """
        self.evaluate_and_create_np_data(substitutions)
        try:
            # self.__swap_compiled_matrices()
            self.xdot_full = self.qp_solver.solve_and_retry(weights=self.np_weights_filtered,
                                                            g=self.np_g_filtered,
                                                            A=self.np_A_filtered,
                                                            lb=self.np_lb_filtered,
                                                            ub=self.np_ub_filtered,
                                                            lbA=self.np_lbA_filtered,
                                                            ubA=self.np_ubA_filtered)
            # self.__swap_compiled_matrices()
            # self._create_debug_pandas()
            return NextCommands(self.free_variables, self.xdot_full)
        except InfeasibleException as e_original:
            if isinstance(e_original, HardConstraintsViolatedException):
                raise
            self.xdot_full = None
            self._create_debug_pandas()
            joint_limits_violated_msg = self._are_joint_limits_violated()
            if joint_limits_violated_msg is not None:
                self.__swap_compiled_matrices()
                try:
                    self.evaluate_and_create_np_data(substitutions)
                    self.xdot_full = self.qp_solver.solve(weights=self.np_weights_filtered,
                                                          g=self.np_g_filtered,
                                                          A=self.np_A_filtered,
                                                          lb=self.np_lb_filtered,
                                                          ub=self.np_ub_filtered,
                                                          lbA=self.np_lbA_filtered,
                                                          ubA=self.np_ubA_filtered)
                    return NextCommands(self.free_variables, self.xdot_full)
                except Exception as e2:
                    # self._create_debug_pandas()
                    # raise OutOfJointLimitsException(self._are_joint_limits_violated())
                    raise OutOfJointLimitsException(joint_limits_violated_msg)
                finally:
                    self.__swap_compiled_matrices()
            #         self.free_variables[0].god_map.get_data(['world']).state.pretty_print()
            self._are_hard_limits_violated(str(e_original))
            self._is_inf_in_data()
            raise

    @profile
    def evaluate_and_create_np_data(self, substitutions):
        self.substitutions = substitutions
        np_big_ass_M = self.compiled_big_ass_M.call2(substitutions)
        self.np_weights = np_big_ass_M[self.A.height, :-2]
        self.np_A = np_big_ass_M[:self.A.height, :self.A.width]
        self.np_lb = np_big_ass_M[self.A.height + 1, :-2]
        self.np_ub = np_big_ass_M[self.A.height + 2, :-2]
        self.np_lbA = np_big_ass_M[:self.A.height, -2]
        self.np_ubA = np_big_ass_M[:self.A.height, -1]

        self.update_filters()
        self.np_weights_filtered = self.np_weights[self.b_filter]
        self.np_g_filtered = np.zeros(self.np_weights_filtered.shape[0])
        self.np_A_filtered = self.np_A[self.bA_filter, :][:, self.b_filter]
        self.np_lb_filtered = self.np_lb[self.b_filter]
        self.np_ub_filtered = self.np_ub[self.b_filter]
        self.np_lbA_filtered = self.np_lbA[self.bA_filter]
        self.np_ubA_filtered = self.np_ubA[self.bA_filter]

    def _are_hard_limits_violated(self, error_message):
        num_non_slack = len(self.free_variables) * self.prediction_horizon * (self.order)
        num_of_slack = len(self.np_lb_filtered) - num_non_slack
        lb = self.np_lb_filtered.copy()
        lb[-num_of_slack:] = -100
        ub = self.np_ub_filtered.copy()
        ub[-num_of_slack:] = 100
        try:
            self.xdot_full = self.qp_solver.solve(weights=self.np_weights_filtered,
                                                  g=self.np_g_filtered,
                                                  A=self.np_A_filtered,
                                                  lb=self.np_lb_filtered,
                                                  ub=self.np_ub_filtered,
                                                  lbA=self.np_lbA_filtered,
                                                  ubA=self.np_ubA_filtered)
        except Exception as e:
            logging.loginfo(f'Can\'t determine if hard constraints are violated: {e}.')
            return False
        else:
            self._create_debug_pandas()
            upper_violations = self.p_xdot[self.p_ub.data < self.p_xdot.data]
            lower_violations = self.p_xdot[self.p_lb.data > self.p_xdot.data]
            if len(upper_violations) > 0 or len(lower_violations) > 0:
                error_message += '\n'
                if len(upper_violations) > 0:
                    error_message += 'upper slack bounds of following constraints might be too low: {}\n'.format(
                        list(upper_violations.index))
                if len(lower_violations) > 0:
                    error_message += 'lower slack bounds of following constraints might be too high: {}'.format(
                        list(lower_violations.index))
                raise HardConstraintsViolatedException(error_message)
        logging.loginfo('No slack limit violation detected.')
    def split_xdot(self, xdot) -> derivative_joint_map:
        split = {}
        offset = len(self.free_variables)
        for derivative in range(Derivatives.velocity, self.order + 1):
            split.update({x.get_symbol(derivative): xdot[i + offset * self.prediction_horizon * (derivative - 1)] for i, x in enumerate(self.free_variables)})
            # split[Derivatives(derivative)] = OrderedDict((x.position_name,
            #                                               xdot[i + offset * self.prediction_horizon * (derivative - 1)])
            #                                              for i, x in enumerate(self.free_variables))
        return split
        return False


    def b_names(self):
        return self.b.names

    def bA_names(self):
        return self.bA.names

    def _viz_mpc(self, joint_name):
        def pad(a, desired_length):
            tmp = np.zeros(desired_length)
            tmp[:len(a)] = a
            return tmp

        sample_period = self.state[str(self.sample_period)]
        try:
            start_pos = self.state[joint_name]
        except KeyError:
            logging.loginfo('start position not found in state')
            start_pos = 0
        ts = np.array([(i + 1) * sample_period for i in range(self.prediction_horizon)])
        filtered_x = self.p_xdot.filter(like='{}'.format(joint_name), axis=0)
        velocities = filtered_x[:self.prediction_horizon].values
        if joint_name in self.state:
            accelerations = filtered_x[self.prediction_horizon:self.prediction_horizon * 2].values
            jerks = filtered_x[self.prediction_horizon * 2:self.prediction_horizon * 3].values
        positions = [start_pos]
        for x_ in velocities:
            positions.append(positions[-1] + x_ * sample_period)

        positions = np.array(positions[1:])
        velocities = pad(velocities.T[0], len(ts))
        positions = pad(positions.T[0], len(ts))

        f, axs = plt.subplots(4, sharex=True)
        axs[0].set_title('position')
        axs[0].plot(ts, positions, 'b')
        axs[0].grid()
        axs[1].set_title('velocity')
        axs[1].plot(ts, velocities, 'b')
        axs[1].grid()
        if joint_name in self.state:
            axs[2].set_title('acceleration')
            axs[2].plot(ts, accelerations, 'b')
            axs[2].grid()
            axs[3].set_title('jerk')
            axs[3].plot(ts, jerks, 'b')
            axs[3].grid()
        plt.tight_layout()
        path, dirs, files = next(os.walk('tmp_data/mpc'))
        file_count = len(files)
        plt.savefig('tmp_data/mpc/mpc_{}_{}.png'.format(joint_name, file_count))

    @profile
    def _create_debug_pandas(self):
        substitutions = self.substitutions
        self.state = {k: v for k, v in zip(self.compiled_big_ass_M.str_params, substitutions)}
        sample_period = self.sample_period
        b_names = self.b_names()
        bA_names = self.bA_names()
        filtered_b_names = np.array(b_names)[self.b_filter]
        filtered_bA_names = np.array(bA_names)[self.bA_filter]
        # H, g, A, lb, ub, lbA, ubA = self.filter_zero_weight_stuff(b_filter, bA_filter)
        # H, g, A, lb, ub, lbA, ubA = self.np_H, self.np_g, self.np_A, self.np_lb, self.np_ub, self.np_lbA, self.np_ubA
        # num_non_slack = len(self.free_variables) * self.prediction_horizon * 3
        # num_of_slack = len(lb) - num_non_slack
        num_vel_constr = len(self.velocity_constraints) * (self.prediction_horizon - 2)
        num_task_constr = len(self.constraints)
        num_constr = num_vel_constr + num_task_constr
        # num_non_slack = l

        # self._eval_debug_exprs()
        p_debug = {}
        for name, value in self.evaluated_debug_expressions.items():
            if isinstance(value, np.ndarray):
                p_debug[name] = value.reshape((value.shape[0] * value.shape[1]))
            else:
                p_debug[name] = np.array(value)
        self.p_debug = pd.DataFrame.from_dict(p_debug, orient='index').sort_index()

        self.p_lb = pd.DataFrame(self.np_lb_filtered, filtered_b_names, ['data'], dtype=float)
        self.p_ub = pd.DataFrame(self.np_ub_filtered, filtered_b_names, ['data'], dtype=float)
        # self.p_g = pd.DataFrame(g, filtered_b_names, ['data'], dtype=float)
        self.p_lbA_raw = pd.DataFrame(self.np_lbA_filtered, filtered_bA_names, ['data'], dtype=float)
        self.p_lbA = deepcopy(self.p_lbA_raw)
        self.p_ubA_raw = pd.DataFrame(self.np_ubA_filtered, filtered_bA_names, ['data'], dtype=float)
        self.p_ubA = deepcopy(self.p_ubA_raw)
        # remove sample period factor
        self.p_lbA[-num_constr:] /= sample_period
        self.p_ubA[-num_constr:] /= sample_period
        self.p_weights = pd.DataFrame(self.np_weights, b_names, ['data'], dtype=float)
        self.p_A = pd.DataFrame(self.np_A_filtered, filtered_bA_names, filtered_b_names, dtype=float)
        if self.xdot_full is not None:
            self.p_xdot = pd.DataFrame(self.xdot_full, filtered_b_names, ['data'], dtype=float)
            # Ax = np.dot(self.np_A, xdot_full)
            # xH = np.dot((self.xdot_full ** 2).T, H)
            # self.p_xH = pd.DataFrame(xH, filtered_b_names, ['data'], dtype=float)
            # p_xg = p_g * p_xdot
            # xHx = np.dot(np.dot(xdot_full.T, H), xdot_full)

            self.p_pure_xdot = deepcopy(self.p_xdot)
            self.p_pure_xdot[-num_constr:] = 0
            self.p_Ax = pd.DataFrame(self.p_A.dot(self.p_xdot), filtered_bA_names, ['data'], dtype=float)
            self.p_Ax_without_slack_raw = pd.DataFrame(self.p_A.dot(self.p_pure_xdot), filtered_bA_names, ['data'],
                                                       dtype=float)
            self.p_Ax_without_slack = deepcopy(self.p_Ax_without_slack_raw)
            self.p_Ax_without_slack[-num_constr:] /= sample_period

        else:
            self.p_xdot = None

        # if self.lbAs is None:
        #     self.lbAs = p_lbA
        # else:
        #     self.lbAs = self.lbAs.T.append(p_lbA.T, ignore_index=True).T
        # self.lbAs.T[[c for c in self.lbAs.T.columns if 'dist' in c]].plot()

        # self.save_all(p_weights, p_A, p_lbA, p_ubA, p_lb, p_ub, p_xdot)

        # self._viz_mpc('j2')
        # self._viz_mpc(self.p_xdot, 'world_robot_joint_state_r_shoulder_lift_joint_position', state)
        # self._viz_mpc(self.p_Ax_without_slack, bA_names[-91][:-2], state)
        # p_lbA[p_lbA != 0].abs().sort_values(by='data')
        # get non 0 A entries
        # p_A.iloc[[1133]].T.loc[p_A.values[1133] != 0]
        # self.save_all_pandas()
        # self._viz_mpc(bA_names[-1])
        pass
