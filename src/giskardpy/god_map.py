import copy
import numbers
from collections import defaultdict
from copy import copy, deepcopy
from multiprocessing import Lock

import numpy as np
from geometry_msgs.msg import Pose, Point, Vector3, PoseStamped, PointStamped, Vector3Stamped

from giskardpy import casadi_wrapper as w, identifier
from giskardpy.data_types import KeyDefaultDict, PrefixName, PrefixDefaultDict
from giskardpy.utils.config_loader import get_namespaces


def get_default_in_override_block(block_identifier, god_map, prefix=None):
    if prefix is not None:
        if identifier.rosparam[0] in block_identifier:
            full_block_identifier = identifier.rosparam + prefix + block_identifier[block_identifier.index(identifier.rosparam[0])+1:]
        else:
            full_block_identifier = prefix + block_identifier
    else:
        full_block_identifier = block_identifier
    try:
        default_value = god_map.get_data(full_block_identifier[:-1] + ['default'])
    except KeyError:
        default_value = god_map.get_data(block_identifier[:-1] + ['default'])
    override = god_map.get_data(full_block_identifier)
    new_override = dict()
    new_default_value = None
    d = dict()
    if isinstance(override, dict):
        if isinstance(default_value, dict):
            new_default_value = dict()
            for key, value in default_value.items():
                if prefix is not None:
                    new_key = PrefixName(key, prefix[0])
                    if type(value) == dict():
                        new_value = dict()
                        for k, v in value.items():
                            new_value[PrefixName(k, prefix[0])] = v
                    else:
                        new_value = value
                    new_default_value[new_key] = new_value
        else:
            new_default_value = default_value
        o = deepcopy(new_default_value)
        for key, value in override.items():
            if type(default_value) == dict():
                o.update(value)
            if prefix is not None:
                new_o = dict()
                if type(o) == dict():
                    for k, v in o.items():
                        if type(k) == PrefixName:
                            new_o[k] = v
                        else:
                            new_o[PrefixName(k, prefix[0])] = v
                else:
                    new_o[PrefixName(key, prefix[0])] = o
                new_override = new_o
            else:
                new_override = override
        d.update(new_override)
    if d:
        ret_d = defaultdict(lambda: new_default_value)
        ret_d.update(d)
    else:
        ret_d = defaultdict(lambda: default_value)
    return ret_d


def set_default_in_override_block(block_identifier, god_map, namespaces):
    d = dict()
    for prefix in namespaces:
        d[prefix] = get_default_in_override_block(block_identifier, god_map, [prefix])
    defaults = dict()
    for prefix in namespaces:
        defaults[PrefixName('default', prefix)] = d[prefix].default_factory()
    new_d = PrefixDefaultDict(lambda p: [v for k, v in defaults.items() if p == k.prefix][0])
    for prefix in namespaces:
        new_d.update(d[prefix])
    god_map.set_data(block_identifier, new_d)
    return KeyDefaultDict(lambda key: god_map.to_symbol(block_identifier + [key]))


def get_member(identifier, member):
    """
    :param identifier:
    :type identifier: Union[None, dict, list, tuple, object]
    :param member:
    :type member: str
    :return:
    """
    try:
        return identifier[member]
    except TypeError:
        if callable(identifier):
            return identifier(*member)
        try:
            return getattr(identifier, member)
        except TypeError:
            pass
    except IndexError:
        return identifier[int(member)]
    except RuntimeError:
        pass


class GetMember(object):
    def __init__(self):
        self.member = None
        self.child = None

    def init_call(self, identifier, data):
        self.member = identifier[0]
        sub_data = self.c(data)
        if len(identifier) == 2:
            self.child = GetMemberLeaf()
            return self.child.init_call(identifier[-1], sub_data)
        elif len(identifier) > 2:
            self.child = GetMember()
            return self.child.init_call(identifier[1:], sub_data)
        return sub_data

    def __call__(self, a):
        return self.c(a)

    def c(self, a):
        try:
            r = a[self.member]
            self.c = self.return_dict
            return r
        except TypeError:
            if callable(a):
                r = a(*self.member)
                self.c = self.return_function_result
                return r
            try:
                r = getattr(a, self.member)
                self.c = self.return_attribute
                return r
            except TypeError:
                pass
        except IndexError:
            r = a[int(self.member)]
            self.c = self.return_list
            return r
        except RuntimeError:
            pass
        raise KeyError(a)

    def return_dict(self, a):
        return self.child.c(a[self.member])

    def return_list(self, a):
        return self.child.c(a[int(self.member)])

    def return_attribute(self, a):
        return self.child.c(getattr(a, self.member))

    def return_function_result(self, a):
        return self.child.c(a(*self.member))


class GetMemberLeaf(object):
    def __init__(self):
        self.member = None
        self.child = None

    def init_call(self, member, data):
        self.member = member
        return self.c(data)

    def __call__(self, a):
        return self.c(a)

    def c(self, a):
        try:
            r = a[self.member]
            self.c = self.return_dict
            return r
        except TypeError:
            if callable(a):
                r = a(*self.member)
                self.c = self.return_function_result
                return r
            try:
                r = getattr(a, self.member)
                self.c = self.return_attribute
                return r
            except TypeError:
                pass
        except IndexError:
            r = a[int(self.member)]
            self.c = self.return_list
            return r
        except RuntimeError:
            pass
        raise KeyError(a)

    def return_dict(self, a):
        return a[self.member]

    def return_list(self, a):
        return a[int(self.member)]

    def return_attribute(self, a):
        return getattr(a, self.member)

    def return_function_result(self, a):
        return a(*self.member)


def get_data(identifier, data):
    """
    :param identifier: Identifier in the form of ['pose', 'position', 'x'],
                       to access class member: robot.joint_state = ['robot', 'joint_state']
                       to access dicts: robot.joint_state['torso_lift_joint'] = ['robot', 'joint_state', ('torso_lift_joint')]
                       to access lists or other indexable stuff: robot.l[-1] = ['robot', 'l', -1]
                       to access functions: lib.str_to_ascii('muh') = ['lib', 'str_to_acii', ['muh']]
                       to access functions without params: robot.get_pybullet_id() = ['robot', 'get_pybullet_id', []]
    :type identifier: list
    :return: object that is saved at key
    """
    try:
        if len(identifier) == 1:
            shortcut = GetMemberLeaf()
            result = shortcut.init_call(identifier[0], data)
        else:
            shortcut = GetMember()
            result = shortcut.init_call(identifier, data)
    except AttributeError as e:
        raise KeyError(e)
    except IndexError as e:
        raise KeyError(e)
    return result, shortcut


class GodMap(object):
    """
    Data structure used by tree to exchange information.
    """

    def __init__(self):
        self._data = {}
        self.expr_separator = '_'
        self.key_to_expr = {}
        self.expr_to_key = {}
        self.last_expr_values = {}
        self.shortcuts = {}
        self.lock = Lock()

    @classmethod
    def init_from_paramserver(cls, node_name):
        import rospy
        from control_msgs.msg import JointTrajectoryControllerState
        from rospy import ROSException
        from giskardpy.utils import logging
        from giskardpy.data_types import order_map

        self = cls()
        self.set_data(identifier.rosparam, rospy.get_param(node_name))
        namespaces = list(set(get_namespaces(self.get_data(identifier.action_server))))
        self.set_data(identifier.rosparam + ['namespaces'], namespaces)
        robot_descriptions = dict()
        for robot_name in namespaces:
            robot_description_topic = PrefixName('robot_description', robot_name)
            robot_descriptions[robot_name] = rospy.get_param('/{}'.format(robot_description_topic))
        self.set_data(identifier.robot_descriptions,  robot_descriptions)
        path_to_data_folder = self.get_data(identifier.data_folder)
        # fix path to data folder
        if not path_to_data_folder.endswith('/'):
            path_to_data_folder += '/'
        self.set_data(identifier.data_folder, path_to_data_folder)

        # while not rospy.is_shutdown():
        #     try:
        #         controlled_joints = rospy.wait_for_message('/whole_body_controller/state',
        #                                                    JointTrajectoryControllerState,
        #                                                    timeout=5.0).joint_names
        #         self.set_data(identifier.controlled_joints, list(sorted(controlled_joints)))
        #     except ROSException as e:
        #         logging.logerr('state topic not available')
        #         logging.logerr(str(e))
        #     else:
        #         break
        #     rospy.sleep(0.5)
        set_default_in_override_block(identifier.external_collision_avoidance, self, namespaces)
        set_default_in_override_block(identifier.self_collision_avoidance, self, namespaces)
        # weights
        for i, key in enumerate(self.get_data(identifier.joint_weights), start=1):
            set_default_in_override_block(identifier.joint_weights + [order_map[i], 'override'], self, namespaces)

        # limits
        for i, key in enumerate(self.get_data(identifier.joint_limits), start=1):
            set_default_in_override_block(identifier.joint_limits + [order_map[i], 'linear', 'override'], self, namespaces)
            set_default_in_override_block(identifier.joint_limits + [order_map[i], 'angular', 'override'], self, namespaces)

        return self

    def __copy__(self):
        god_map_copy = GodMap()
        god_map_copy._data = copy(self._data)
        god_map_copy.key_to_expr = copy(self.key_to_expr)
        god_map_copy.expr_to_key = copy(self.expr_to_key)
        return god_map_copy

    def __enter__(self):
        self.lock.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.lock.release()

    def unsafe_get_data(self, identifier):
        """

        :param identifier: Identifier in the form of ['pose', 'position', 'x'],
                           to access class member: robot.joint_state = ['robot', 'joint_state']
                           to access dicts: robot.joint_state['torso_lift_joint'] = ['robot', 'joint_state', ('torso_lift_joint')]
                           to access lists or other indexable stuff: robot.l[-1] = ['robot', 'l', -1]
                           to access functions: lib.str_to_ascii('muh') = ['lib', 'str_to_acii', ['muh']]
                           to access functions without params: robot.get_pybullet_id() = ['robot', 'get_pybullet_id', []]
        :type identifier: list
        :return: object that is saved at key
        """
        identifier = tuple(identifier)
        try:
            if identifier not in self.shortcuts:
                result, shortcut = get_data(identifier, self._data)
                if shortcut:
                    self.shortcuts[identifier] = shortcut
                return result
            return self.shortcuts[identifier].c(self._data)
        except Exception as e:
            e2 = type(e)('{}; path: {}'.format(e, identifier))
            raise e2

    def get_data(self, identifier):
        with self.lock:
            r = self.unsafe_get_data(identifier)
        return r

    def clear_cache(self):
        # TODO should be possible without clear cache
        self.shortcuts = {}

    def to_symbol(self, identifier):
        """
        All registered identifiers will be included in self.get_symbol_map().
        :type identifier: list
        :return: the symbol corresponding to the identifier
        :rtype: sw.Symbol
        """
        assert isinstance(identifier, list) or isinstance(identifier, tuple)
        identifier = tuple(identifier)
        identifier_parts = identifier
        if identifier not in self.key_to_expr:
            expr = w.Symbol(self.expr_separator.join([str(x) for x in identifier]))
            if expr in self.expr_to_key:
                raise Exception('{} not allowed in key'.format(self.expr_separator))
            self.key_to_expr[identifier] = expr
            self.expr_to_key[str(expr)] = identifier_parts
        return self.key_to_expr[identifier]

    def to_expr(self, identifier):
        data = self.get_data(identifier)
        if isinstance(data, np.ndarray):
            data = data.tolist()
        if isinstance(data, numbers.Number):
            return self.to_symbol(identifier)
        if isinstance(data, Pose):
            return self.pose_msg_to_frame(identifier)
        elif isinstance(data, PoseStamped):
            return self.pose_msg_to_frame(identifier + ['pose'])
        elif isinstance(data, Point):
            return self.point_msg_to_point3(identifier)
        elif isinstance(data, PointStamped):
            return self.point_msg_to_point3(identifier + ['point'])
        elif isinstance(data, Vector3):
            return self.vector_msg_to_vector3(identifier)
        elif isinstance(data, Vector3Stamped):
            return self.vector_msg_to_vector3(identifier + ['vector'])
        elif isinstance(data, list):
            return self.list_to_symbol_matrix(identifier, data)
        elif isinstance(data, np.ndarray):
            return self.list_to_symbol_matrix(identifier, data)
        else:
            raise NotImplementedError('to_expr not implemented for type {}.'.format(type(data)))

    def list_to_symbol_matrix(self, identifier, data):
        def replace_nested_list(l, f, start_index=None):
            if start_index is None:
                start_index = []
            result = []
            for i, entry in enumerate(l):
                index = start_index + [i]
                if isinstance(entry, list):
                    result.append(replace_nested_list(entry, f, index))
                else:
                    result.append(f(index))
            return result
        return w.Matrix(replace_nested_list(data, lambda index: self.to_symbol(identifier + index)))

    def list_to_point3(self, identifier):
        return w.point3(
            x=self.to_symbol(identifier + [0]),
            y=self.to_symbol(identifier + [1]),
            z=self.to_symbol(identifier + [2]),
        )

    def list_to_vector3(self, identifier):
        return w.vector3(
            x=self.to_symbol(identifier + [0]),
            y=self.to_symbol(identifier + [1]),
            z=self.to_symbol(identifier + [2]),
        )

    def list_to_translation3(self, identifier):
        return w.translation3(
            x=self.to_symbol(identifier + [0]),
            y=self.to_symbol(identifier + [1]),
            z=self.to_symbol(identifier + [2]),
        )

    def list_to_frame(self, identifier):
        return w.Matrix(
            [
                [
                    self.to_symbol(identifier + [0, 0]),
                    self.to_symbol(identifier + [0, 1]),
                    self.to_symbol(identifier + [0, 2]),
                    self.to_symbol(identifier + [0, 3])
                ],
                [
                    self.to_symbol(identifier + [1, 0]),
                    self.to_symbol(identifier + [1, 1]),
                    self.to_symbol(identifier + [1, 2]),
                    self.to_symbol(identifier + [1, 3])
                ],
                [
                    self.to_symbol(identifier + [2, 0]),
                    self.to_symbol(identifier + [2, 1]),
                    self.to_symbol(identifier + [2, 2]),
                    self.to_symbol(identifier + [2, 3])
                ],
                [
                    0, 0, 0, 1
                ],
            ]
        )

    def pose_msg_to_frame(self, identifier):
        return w.frame_quaternion(
            x=self.to_symbol(identifier + ['position', 'x']),
            y=self.to_symbol(identifier + ['position', 'y']),
            z=self.to_symbol(identifier + ['position', 'z']),
            qx=self.to_symbol(identifier + ['orientation', 'x']),
            qy=self.to_symbol(identifier + ['orientation', 'y']),
            qz=self.to_symbol(identifier + ['orientation', 'z']),
            qw=self.to_symbol(identifier + ['orientation', 'w']),
        )

    def point_msg_to_point3(self, identifier):
        return w.point3(
            x=self.to_symbol(identifier + ['x']),
            y=self.to_symbol(identifier + ['y']),
            z=self.to_symbol(identifier + ['z']),
        )

    def vector_msg_to_vector3(self, identifier):
        return w.vector3(
            x=self.to_symbol(identifier + ['x']),
            y=self.to_symbol(identifier + ['y']),
            z=self.to_symbol(identifier + ['z']),
        )

    def get_values(self, symbols):
        """
        :return: a dict which maps all registered expressions to their values or 0 if there is no number entry
        :rtype: list
        """
        # TODO potential speedup by only updating entries that have changed
        # its a trap, this function only looks slow with lineprofiler
        with self.lock:
            return self.unsafe_get_values(symbols)

    def unsafe_get_values(self, symbols):
        """
        :return: a dict which maps all registered expressions to their values or 0 if there is no number entry
        :rtype: list
        """
        return [self.unsafe_get_data(self.expr_to_key[expr]) for expr in symbols]

    def evaluate_expr(self, expr):
        fs = w.free_symbols(expr)
        fss = [str(s) for s in fs]
        f = w.speed_up(expr, fs)
        result = f.call2(self.get_values(fss))
        if len(result) == 1:
            return result[0][0]
        else:
            return result

    def get_registered_symbols(self):
        """
        :rtype: list
        """
        return self.key_to_expr.values()

    def unsafe_set_data(self, identifier, value):
        """

        :param identifier: e.g. ['pose', 'position', 'x']
        :type identifier: list
        :param value:
        :type value: object
        """
        if len(identifier) == 0:
            raise ValueError('key is empty')
        namespace = identifier[0]
        if namespace not in self._data:
            if len(identifier) > 1:
                raise KeyError('Can not access member of unknown namespace: {}'.format(identifier))
            else:
                self._data[namespace] = value
        else:
            result = self._data[namespace]
            for member in identifier[1:-1]:
                result = get_member(result, member)
            if len(identifier) > 1:
                member = identifier[-1]
                if isinstance(result, dict):
                    result[member] = value
                elif isinstance(result, list):
                    result[int(member)] = value
                else:
                    setattr(result, member, value)
            else:
                self._data[namespace] = value

    def set_data(self, identifier, value):
        with self.lock:
            self.unsafe_set_data(identifier, value)
