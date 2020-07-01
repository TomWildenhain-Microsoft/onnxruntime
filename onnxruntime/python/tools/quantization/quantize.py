# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------
import os
import onnx
import onnx.numpy_helper
import struct

import numpy as np
from onnx import onnx_pb as onnx_proto
from onnx import shape_inference
from .registry import CreateOpQuantizer, CreateDefaultOpQuantizer
from .quant_utils import *

__producer__ = "onnx.quantize"
__version__ = "0.1.0"

def quantize_data(data, quantize_range, qType):
    '''
        :parameter data: data to quantize
        :parameter quantize_range: list of data to weight pack.
        :parameter qType: data type to quantize to. Supported types UINT8 and INT8
        :return: minimum, maximum, zero point, scale, and quantized weights

        To pack weights, we compute a linear transformation
            - when data type == uint8 mode, from [rmin, rmax] -> [0, 2^{b-1}] and
            - when data type == int8, from [-m , m] -> [-(2^{b-1}-1), 2^{b-1}-1] where
                m = max(abs(rmin), abs(rmax))

        and add necessary intermediate nodes to trasnform quantized weight to full weight using the equation
        r = S(q-z), where
            r: real original value
            q: quantized value
            S: scale
            z: zero point
    '''
    rmin = min(min(data), 0)
    rmax = max(max(data), 0)

    if qType == onnx_proto.TensorProto.INT8:
        max_range = max(abs(rmin), abs(rmax))
        scale = (float(max_range) * 2) / quantize_range
        zero_point = 0
        # signed byte type
        quantized_data = (np.asarray(data) / scale).round().astype('b')
    elif qType == onnx_proto.TensorProto.UINT8:
        scale = (float(rmax) - rmin) / quantize_range if rmin != rmax else 1
        zero_point = round((0 - rmin) / scale)  # round to nearest integer
        quantized_data = ((np.asarray(data) / scale).round() + zero_point).astype('B')  # unsigned byte type
    else:
        raise ValueError("Unexpected data type {} requested. Only INT8 and UINT8 are supported.".format(qType))

    return rmin, rmax, zero_point, scale, quantized_data


def _add_initializer_if_not_present(graph, name, value, shape, type):
    '''
    Helper function to add an initializer if it is not present in the graph.
        parameter graph: GraphProto.
        parameter name: Initializer's name.
        parameter value: Initializer's value.
        parameter shape: Initializer's shape.
        parameter type: Initializer's type.
    '''
    if find_by_name(name, graph.initializer) is None:
        initializer = onnx.helper.make_tensor(name, type, shape, value)
        graph.initializer.extend([initializer])


def _get_qrange_for_qType(qType):
    '''
    Helper function to get the quantization range for a type.
        parameter qType: quantization type.
        return: quantization range.
    '''
    if qType == onnx_proto.TensorProto.UINT8:
        return 255  # 2^b - 1
    elif qType == onnx_proto.TensorProto.INT8:
        return 254  # [-(2^{b-1}-1), 2^{b-1}-1]: [-127, 127] for 8 bits.
    else:
        raise ValueError('unsupported quantization data type')


def _find_nodes_using_initializer(nodes, initializer):
    '''
    Helper function to find all nodes with an initializer as a input.
        parameter nodes: node list.
        parameter initializer: Initializer in TensorProto format.
        return: List of nodes.
    '''
    result = []
    for node in nodes:
        for node_input in node.input:
            if node_input == initializer.name:
                result.append(node)
    return result


class ONNXQuantizer:
    def __init__(self, model, per_channel, mode, static, fuse_dynamic_quant, weight_qType, input_qType,
                 quantization_params, nodes_to_quantize, nodes_to_exclude):
        self.model = shape_inference.infer_shapes(model)
        self.value_infos = {vi.name: vi for vi in self.model.graph.value_info}
        self.per_channel = per_channel  # weight-pack per channel
        self.mode = mode  # QuantizationMode.Value
        self.static = static  # use static quantization for inputs.
        self.fuse_dynamic_quant = fuse_dynamic_quant
        self.input_qType = input_qType  # quantize input type
        self.weight_qType = weight_qType  # quantize data type
        self.quantization_params = quantization_params
        self.nodes_to_quantize = nodes_to_quantize  # specific nodes to quantize
        self.nodes_to_exclude = nodes_to_exclude  # specific nodes to exclude
        self.nodes = []

        if not self.mode in quantization_modes:
            raise ValueError('unsupported quantization mode {}'.format(self.mode))

        # QuantizeRange tensor name and zero tensor name for scale and zero point calculation.
        # Used when static is False
        self.fixed_qrange_uint8_name = "fixed_quantization_range_uint8"
        self.fixed_qrange_int8_name = "fixed_quantization_range_int8"
        # For uint8 data-type, to compute zero point, we subtract rmin from 0 (represented by fixed_zero_name tensor)
        self.fixed_zero_name = "fixed_zero"
        # For int8 data-type, zero point is always zero (respresented by fixed_zero_point_name tensor)
        self.fixed_zero_zp_name = "fixed_zero_zp"

        # List of quantized weights
        self._quantized_weights = []
        # Map of all original value names to quantized value names
        self.quantized_value_map = {}

    def preprocess(self):

        nodes_to_remove = []
        nodes_to_add = []
        for node in self.model.graph.node:
            if node.op_type == 'Gemm':
                alpha = 1.0
                beta = 1.0
                transA = 0
                transB = 0
                for attr in node.attribute:
                    if attr.name == 'alpha':
                        alpha = onnx.helper.get_attribute_value(attr)
                    elif attr.name == 'beta':
                        beta = onnx.helper.get_attribute_value(attr)
                    elif attr.name == 'transA':
                        transA = onnx.helper.get_attribute_value(attr)
                    elif attr.name == 'transB':
                        transB = onnx.helper.get_attribute_value(attr)
                if alpha == 1.0 and beta == 1.0 and transA == 0 and transB == 0:
                    matmul_node = onnx.helper.make_node(
                        'MatMul',
                        [node.input[0], node.input[1]],
                        [node.output[0]+'_MatMul'],
                        name=node.output[0]+'_MatMul')

                    add_node = onnx.helper.make_node(
                        'Add',
                        inputs=[node.output[0]+'_MatMul', node.input[2]],
                        outputs=node.output,
                        name=node.output[0]+'_Add')
                    
                    nodes_to_remove.extend([node])
                    nodes_to_add.extend([matmul_node, add_node])

        self.model.graph.node.extend(nodes_to_add)
        for node in nodes_to_remove:
            self.model.graph.node.remove(node)

    def quantize_model(self):
        self.preprocess()
        # Create a new topologically sorted list for quantizing a model
        self.nodes = []
        for node in self.model.graph.node:
            # if a list of ops to be quantized is provided then only quantize those ops
            if self.nodes_to_quantize is not None and node.name not in self.nodes_to_quantize:
                op_quantizer = CreateDefaultOpQuantizer(self, node)
            elif self.nodes_to_exclude is not None and node.name in self.nodes_to_exclude:
                op_quantizer = CreateDefaultOpQuantizer(self, node)
            else:
                op_quantizer = CreateOpQuantizer(self, node)
            op_quantizer.quantize()

        self._dequantize_outputs()

        # update weights
        self._update_nodes_using_weight()

        # extend is used to append to the list for a protobuf fields
        # https://developers.google.com/protocol-buffers/docs/reference/python-generated?csw=1#fields
        self.model.graph.ClearField('node')
        self.model.graph.node.extend(self.nodes)

        # 
        # Remove weights which are already quantized from graph.
        self._remove_quantized_weights()

        # update opset.
        opset_info = next(
            (opset for opset in self.model.opset_import if opset.domain == '' or opset.domain == onnx_domain), None)
        if opset_info is not None:
            self.model.opset_import.remove(opset_info)
        self.model.opset_import.extend([onnx.helper.make_opsetid(onnx_domain, onnx_op_set_version)])

        return self.model

    def find_weight_data(self, initializer):
        '''
            :param initializer: TensorProto initializer object from a graph
            :return: a list of initialized data in a given initializer object
        '''
        if initializer.data_type == onnx_proto.TensorProto.FLOAT:
            weights = onnx.numpy_helper.to_array(initializer)
        else:
            raise ValueError('Only float type quantization is supported. Weights {} is {}. '.format(
                initializer.name, type_to_name[initializer.data_type]))
        return weights

    def is_valid_quantize_weight(self, weight_name):
        weight = find_by_name(weight_name, self.model.graph.initializer)
        return weight is not None and weight.data_type == onnx_proto.TensorProto.FLOAT

    def _is_valid_quantize_value(self, value_name):
        if value_name in self.value_infos:
            value_info = self.value_infos[value_name]
            return value_info.type.HasField(
                'tensor_type') and value_info.type.tensor_type.elem_type == onnx_proto.TensorProto.FLOAT
        weight = find_by_name(value_name, self.model.graph.initializer)
        return weight is not None and weight.data_type == onnx_proto.TensorProto.FLOAT

    def _remove_quantized_weights(self):
        ''' Remove the weights which are already quantized from graph initializer list.
            This function assumes that after quantization, all nodes that previously use a weight:
                - use output from DequantizeLinear as input if they do not support quantization.
                - use quantized weight if they support quantization.
        '''
        for weight in self._quantized_weights:
            # Remove existing weight initializer
            self.model.graph.initializer.remove(weight.initializer)

            # Removing input weight to a convolution
            try:
                weight_input = next(val for val in self.model.graph.input if val.name == weight.name)
                self.model.graph.input.remove(weight_input)
            except StopIteration:
                if self.model.ir_version < 4:
                    print("Warning: invalid weight name {} found in the graph (not a graph input)".format(weight.name))

    def _update_graph(self, weight):
        '''
            Given a weight object, update the graph by doing the following:
             - remove old initializer, update new initializers for quantized weight, zero point, and scale
             - remove old weight input, update with new inputs for quantized weight, zero point, and scale
            This function does NOT update the nodes in the graph, just initializers and inputs
        '''
        quantized_value = self.quantized_value_map[weight.name]
        assert (quantized_value is not None)
        packed_weight_name = quantized_value.q_name
        scale_name = quantized_value.scale_name
        zero_point_name = quantized_value.zp_name

        # Update packed weight, zero point, and scale initializers
        packed_weight_np_data = np.asarray(weight.quantized_data,
                                           dtype=onnx.mapping.TENSOR_TYPE_TO_NP_TYPE[weight.qType]).reshape(
                                               weight.initializer.dims)
        packed_weight_initializer = onnx.numpy_helper.from_array(packed_weight_np_data, packed_weight_name)

        if weight.axis is not None:
            zero_scale_shape = [weight.initializer.dims[weight.axis]]
        else:  # scale and zero point must be scalar
            zero_scale_shape = []
        zero_point_type = weight.qType
        scale_initializer = onnx.helper.make_tensor(scale_name, onnx_proto.TensorProto.FLOAT, zero_scale_shape,
                                                    weight.scales)
        zero_initializer = onnx.helper.make_tensor(zero_point_name, zero_point_type, zero_scale_shape,
                                                   weight.zero_points)

        self.model.graph.initializer.extend([packed_weight_initializer, scale_initializer, zero_initializer])

        self._quantized_weights.append(weight)

    def _get_quantized_weight(self, initializer, qType):
        '''
            :param initializer: TensorProto initializer
            :param qType: type to quantize to
            :return: Weight class with quantization information
        '''
        weights_data = self.find_weight_data(initializer)
        rmin, rmax, zero_point, scale, quantized_weights_data = quantize_data(weights_data.flatten().tolist(),
                                                                              _get_qrange_for_qType(qType), qType)
        weight = QuantizedInitializer(initializer.name,
                                      initializer, [rmin], [rmax], [zero_point], [scale],
                                      weights_data,
                                      quantized_weights_data,
                                      axis=None,
                                      qType=qType)

        # Log entry for this quantized weight
        assert (weight.name not in self.quantized_value_map)
        quantized_value = QuantizedValue(weight.name, weight.name + "_quantized", weight.name + "_scale",
                                         weight.name + "_zero_point", QuantizedValueType.Initializer, None, qType)
        self.quantized_value_map[weight.name] = quantized_value

        return weight

    def _get_quantized_weight_convolution(self, initializer, qType):
        '''
            :param initializer: initializer TypeProto to quantize
            :param qType: type to quantize to
            :return: Weight class object with quantization information for a given initializer
        '''
        if not self.per_channel:
            return self._get_quantized_weight(initializer, qType)

        weights = self.find_weight_data(initializer)
        # Quantize per output channel
        # Assuming (M x C/group x kH x kW) format where M is number of output channels.
        channel_count = initializer.dims[0]
        np_data = np.reshape(weights, initializer.dims)
        rmin_list = []
        rmax_list = []
        zero_point_list = []
        scale_list = []
        quantized_per_channel_data_list = []
        for i in range(channel_count):
            # for each channel, compute quantization data. Assuming (M x C/group x kH x kW)
            per_channel_data = np_data[i, :, :, :].flatten()
            rmin, rmax, zero_point, scale, quantized_per_channel_data = quantize_data(
                per_channel_data.flatten().tolist(), _get_qrange_for_qType(qType), qType)
            rmin_list.append(rmin)
            rmax_list.append(rmax)
            zero_point_list.append(zero_point)
            scale_list.append(scale)
            quantized_per_channel_data_list.append(quantized_per_channel_data)
        channel_index = 0  # (M x C/group x kH x kW)
        # combine per_channel_data into one
        reshape_dims = list(initializer.dims)  # deep copy
        reshape_dims[channel_index] = 1  # only one per channel for reshape
        quantized_weights = np.asarray(quantized_per_channel_data_list[0]).reshape(reshape_dims)
        for i in range(1, len(quantized_per_channel_data_list)):
            channel_weights = np.asarray(quantized_per_channel_data_list[i]).reshape(reshape_dims)
            quantized_weights = np.concatenate((quantized_weights, channel_weights), axis=0)

        weight = QuantizedInitializer(initializer.name, initializer, rmin_list, rmax_list, zero_point_list, scale_list,
                                      weights,
                                      quantized_weights.flatten().tolist(), channel_index, qType)

        # Make entry for this quantized weight
        assert (weight.name not in self.quantized_value_map)
        quantized_value = QuantizedValue(weight.name, weight.name + "_quantized", weight.name + "_scale",
                                         weight.name + "_zero_point", QuantizedValueType.Initializer, None, qType)
        self.quantized_value_map[weight.name] = quantized_value

        return weight

    def _get_dynamic_input_quantization_params(self, input_name, nodes_list, qType):
        '''
        Create nodes for dynamic quantization of input and add them to nodes_list.
            parameter input_name: Name of the input.
            parameter nodes_list: new nodes are appended to this list.
            parameter qType: type to quantize to.
            return: scale_name, zero_point_name, scale_shape, zero_point_shape.
        '''
        if qType == onnx_proto.TensorProto.INT8:
            return self._get_dynamic_input_quantization_params_int8(input_name, nodes_list)

        return self._get_dynamic_input_quantization_params_uint8(input_name, nodes_list)

    def _get_dynamic_input_quantization_params_int8(self, input_name, nodes_list):
        '''
        Create nodes for dynamic quantization of input to int8 and add them to nodes_list
            parameter input_name: Name of the input.
            parameter nodes_list: new nodes are appended to this list.
            return: scale_name, zero_point_name, scale_shape, zero_point_shape.
        '''
        qType = onnx_proto.TensorProto.INT8

        # Reduce min and Reduce max
        input_scale_name = input_name + "_scale"

        reduce_min_name = input_name + "_ReduceMin"
        reduce_min_node = onnx.helper.make_node("ReduceMin", [input_name], [reduce_min_name + ":0"],
                                                reduce_min_name,
                                                keepdims=0)
        nodes_list.append(reduce_min_node)

        reduce_max_name = input_name + "_ReduceMax"
        reduce_max_node = onnx.helper.make_node("ReduceMax", [input_name], [reduce_max_name + ":0"],
                                                reduce_max_name,
                                                keepdims=0)
        nodes_list.append(reduce_max_node)

        # Compute scale
        #   Find abs(rmin)
        reduce_min_abs_name = reduce_min_name + "_Abs"
        reduce_min_abs_node = onnx.helper.make_node("Abs", [reduce_min_node.output[0]], [reduce_min_abs_name + ":0"],
                                                    reduce_min_abs_name)
        nodes_list.append(reduce_min_abs_node)
        #   Find abs(rmax)
        reduce_max_abs_name = reduce_max_name + "_Abs"
        reduce_max_abs_node = onnx.helper.make_node("Abs", [reduce_max_node.output[0]], [reduce_max_abs_name + ":0"],
                                                    reduce_max_abs_name)
        nodes_list.append(reduce_max_abs_node)
        #   Compute max of abs(rmin) and abs(rmax)
        abs_max_name = input_name + "_Abs_Max"
        abs_max_node = onnx.helper.make_node("Max", [reduce_min_abs_node.output[0], reduce_max_abs_node.output[0]],
                                             [abs_max_name + ":0"], abs_max_name)
        nodes_list.append(abs_max_node)
        #   and divide by (quantize_range/2.0) which will be equal to max(...)*2.0/quantize_range
        _add_initializer_if_not_present(self.model.graph, self.fixed_qrange_int8_name,
                                        [_get_qrange_for_qType(qType) / 2.0], [], onnx_proto.TensorProto.FLOAT)
        scale_div_name = input_name + "scale_Div"
        scale_div_node = onnx.helper.make_node("Div", [abs_max_node.output[0], self.fixed_qrange_int8_name],
                                               [input_scale_name], scale_div_name)
        nodes_list.append(scale_div_node)

        # Zero point
        _add_initializer_if_not_present(self.model.graph, self.fixed_zero_zp_name, [0], [], qType)

        return input_scale_name, self.fixed_zero_zp_name, [], []

    def _get_dynamic_input_quantization_params_uint8(self, input_name, nodes_list):
        '''
        Create nodes for dynamic quantization of input to uint8 and add them to nodes_list
            parameter input_name: Name of the input.
            parameter nodes_list: new nodes are appended to this list.
            return: scale_name, zero_point_name, scale_shape, zero_point_shape.
        '''
        qType = onnx_proto.TensorProto.UINT8
        # Reduce min and Reduce max
        input_scale_name = input_name + "_scale"
        input_zp_name = input_name + "_zero_point"

        reduce_min_name = input_name + "_ReduceMin"
        reduce_min_node = onnx.helper.make_node("ReduceMin", [input_name], [reduce_min_name + ":0"],
                                                reduce_min_name,
                                                keepdims=0)
        nodes_list.append(reduce_min_node)

        reduce_max_name = input_name + "_ReduceMax"
        reduce_max_node = onnx.helper.make_node("ReduceMax", [input_name], [reduce_max_name + ":0"],
                                                reduce_max_name,
                                                keepdims=0)
        nodes_list.append(reduce_max_node)

        # Add tensors for quantize range and zero value.
        _add_initializer_if_not_present(self.model.graph, self.fixed_qrange_uint8_name, [_get_qrange_for_qType(qType)],
                                        [], onnx_proto.TensorProto.FLOAT)
        _add_initializer_if_not_present(self.model.graph, self.fixed_zero_name, [0.0], [], onnx_proto.TensorProto.FLOAT)

        # Compute Scale
        #   Subtract rmax and rmin
        scale_sub_name = input_name + "_scale_Sub"
        scale_sub_node = onnx.helper.make_node("Sub", [reduce_max_node.output[0], reduce_min_node.output[0]],
                                               [scale_sub_name + ":0"], scale_sub_name)
        nodes_list.append(scale_sub_node)
        #   and divide by quantize range
        scale_div_name = input_name + "_scale_Div"
        scale_div_node = onnx.helper.make_node("Div", [scale_sub_node.output[0], self.fixed_qrange_uint8_name],
                                               [input_scale_name], scale_div_name)
        nodes_list.append(scale_div_node)

        # Compute zero point
        #   Subtract zero and rmin
        zp_sub_name = input_name + "_zero_point_Sub"
        zp_sub_node = onnx.helper.make_node("Sub", [self.fixed_zero_name, reduce_min_node.output[0]],
                                            [zp_sub_name + ":0"], zp_sub_name)
        nodes_list.append(zp_sub_node)
        #   Divide by scale
        zp_div_name = input_name + "_zero_point_Div"
        zp_div_node = onnx.helper.make_node("Div", [zp_sub_node.output[0], input_scale_name], [zp_div_name + ":0"],
                                            zp_div_name)
        nodes_list.append(zp_div_node)
        #   Compute floor
        zp_floor_name = input_name + "_zero_point_Floor"
        zp_floor_node = onnx.helper.make_node("Floor", zp_div_node.output, [zp_floor_name + ":0"], zp_floor_name)
        nodes_list.append(zp_floor_node)
        #   Cast to integer
        zp_cast_name = input_name + "_zero_point_Cast"
        zp_cast_node = onnx.helper.make_node("Cast", zp_floor_node.output, [input_zp_name], zp_cast_name, to=qType)
        nodes_list.append(zp_cast_node)

        return input_scale_name, input_zp_name, [], []

    def get_quantization_params(self, param_name):
        '''
        Create initializers and inputs in the graph for zero point and scale of output.
        Zero point and scale values are obtained from self.quantization_params if specified.

            parameter param_name: Name of the quantization parameter.
            return: result, scale_name, zero_point_name, scale_shape, zero_point_shape.
        '''
        if self.quantization_params is None or param_name not in self.quantization_params:
            return False, "", "", "", ""
        params = self.quantization_params[param_name]
        if params is None or len(params) != 2:
            raise ValueError("Quantization parameters should contain zero point and scale. "
                             "Specified values for output {}: {}".format(param_name, params))

        if not np.isscalar(params[0]):
            raise ValueError("Zero point for param {} should be a scalar value. Value specified: {}".format(
                param_name, params[0]))
        if not np.isscalar(params[1]):
            raise ValueError("Scale for param {} should be a scalar value. Value specified: {}".format(
                param_name, params[1]))

        zero_point_values = [params[0].item()]
        zero_point_shape = []
        zero_point_name = param_name + "_zero_point"
        zero_point_type = onnx.mapping.NP_TYPE_TO_TENSOR_TYPE[params[0].dtype]

        scale_values = [params[1].item()]
        scale_shape = []
        scale_name = param_name + "_scale"

        # Add initializers
        _add_initializer_if_not_present(self.model.graph, zero_point_name, zero_point_values, zero_point_shape,
                                        zero_point_type)
        _add_initializer_if_not_present(self.model.graph, scale_name, scale_values, scale_shape,
                                        onnx_proto.TensorProto.FLOAT)

        return True, scale_name, zero_point_name, scale_shape, zero_point_shape

    def _get_quantize_input_nodes(self, node, input_index, qType):
        '''
        Given a input for a node (which is not a initializer), this function
            - add nodes to compute zero point and scale for this input if they don't exist.
            - add new QuantizeLinear node to quantize the input.

            parameter node: node being quantized in NodeProto format.
            parameter input_index: index of input in node.input.
            parameter qType: type to quantize to.
            return: List of newly created nodes in NodeProto format.
        '''
        input_name = node.input[input_index]
        output_name = input_name + "_quantized"

        data_found, scale_name, zp_name, _, _ = \
            self.get_quantization_params(input_name)

        if self.static:
            if data_found == False:
                raise ValueError(
                    "Quantization parameters are not specified for param {}."
                    "In static mode quantization params for inputs and outputs of nodes to be quantized are required.".
                    format(input_name))

            qlinear_node = onnx.helper.make_node("QuantizeLinear", [input_name, scale_name, zp_name], [output_name],
                                                 input_name + "_QuantizeLinear")
            return [qlinear_node]

        else:
            if data_found == True:
                qlinear_node = onnx.helper.make_node("QuantizeLinear", [input_name, scale_name, zp_name], [output_name],
                                                     input_name + "_QuantizeLinear")
                return [qlinear_node]
            else:
                # Scale and Zero Points not available for this input. Add nodes to dynamically compute it
                if self.fuse_dynamic_quant and qType == onnx_proto.TensorProto.UINT8:
                    scale_name = input_name + "_scale"
                    zeropoint_name = input_name + "_zero_point"
                    qlinear_node = onnx.helper.make_node("DynamicQuantizeLinear", [input_name],
                                                         [output_name, scale_name, zeropoint_name],
                                                         input_name + "_QuantizeLinear")
                    return [qlinear_node]

                else:
                    nodes = []
                    scale_name, zp_name, scale_shape, zp_shape = \
                        self._get_dynamic_input_quantization_params(
                            input_name, nodes, qType)
                    qlinear_node = onnx.helper.make_node("QuantizeLinear", [input_name, scale_name, zp_name],
                                                         [output_name], input_name + "_QuantizeLinear")

                    return nodes + [qlinear_node]

    def get_bias_add_nodes(self, nodes, node, last_output, quantized_bias_name):
        '''
        Given a node, this function handles bias add by adding a "reshape" node on bias and an "add" node

            parameter nodes: new nodes would be appended into nodes
            parameter node: current node (Conv)
            parameter last_output: output of previous node (input to bias add)
            return: the name of output
        '''
        # Add an Add operation for bias
        # Add reshape for correct broadcase
        reshape_input = [quantized_bias_name]

        # Add tensors for the shape to be reshaped to
        _add_initializer_if_not_present(self.model.graph, "reshape_shape", [1, -1, 1, 1], [4],
                                        onnx_proto.TensorProto.INT64)
        reshape_input.append('reshape_shape')
        reshape_op_output = node.output[0] + "_reshape"
        reshape_node = onnx.helper.make_node("Reshape", reshape_input, [reshape_op_output],
                                             quantized_bias_name + "reshape")
        nodes.append(reshape_node)

        bias_add_input = [last_output]
        bias_add_input.append(reshape_op_output)
        add_node_output = node.output[0] + "_bias_add"
        add_node = onnx.helper.make_node("Add", bias_add_input, [add_node_output], quantized_bias_name + "bias_add")
        nodes.append(add_node)
        return add_node_output

    def _update_nodes_using_weight(self):
        '''Find all nodes using a weight that do not support quantization and
        add a DequantizeLinear node before those nodes. This includes all nodes except Conv, MatMul.

            parameter weight: Weight object
            parameter new_nodes_list: List of new nodes created before processing current node.
            return: List of new nodes created.
        '''
        nodes_list = []
        for weight in self._quantized_weights:
            nodes_using_weight = _find_nodes_using_initializer(self.nodes, weight.initializer)

            dequantize_linear_name = weight.name + "_DequantizeLinear"
            output_name = weight.name + "_dequantized"

            # Check if DequantizeLinear node needs to be added to graph.
            if len(nodes_using_weight) != 0 and \
                    find_by_name(dequantize_linear_name, self.nodes) is None:
                inputs = [weight.name + "_quantized", weight.name + "_scale", weight.name + "_zero_point"]
                node = onnx.helper.make_node("DequantizeLinear", inputs, [output_name], dequantize_linear_name)
                nodes_list.append(node)

            # Update unsupported nodes to take dequantized weight as input.
            for node in nodes_using_weight:
                for i, node_input in enumerate(node.input):
                    if node_input == weight.name:
                        node.input[i] = output_name

        self.nodes += nodes_list

    def _dynamic_quantize_bias(self, input_name, weight_scale_name, bias_name, quantized_bias_name, new_node_list):
        '''
        Adds series of nodes required to quantize the bias dynamically.
            parameter input_name: Input name
            parameter weight_scale_name: Weight scale.
            parameter bias_scale_name: Bias to quantize.
            parameter quantied_bias_name: Output name to use for quantized bias.
        '''
        qType = onnx_proto.TensorProto.INT32

        input_scale_name = input_name + "_scale"
        bias_scale_node = onnx.helper.make_node("Mul", [input_scale_name, weight_scale_name], [bias_name + "_scale"],
                                                bias_name + "_scale_node")
        new_node_list.append(bias_scale_node)

        quantize_bias_node = onnx.helper.make_node("Div", [bias_name, bias_scale_node.output[0]],
                                                   [bias_name + "_tmp_quant:0"], bias_name + "_tmp_qaunt")
        new_node_list.append(quantize_bias_node)

        bias_rounded_node = onnx.helper.make_node("Floor", quantize_bias_node.output, [bias_name + "_quant_rounded:0"],
                                                  bias_name + "_quant_rounded")
        new_node_list.append(bias_rounded_node)

        bias_cast_node = onnx.helper.make_node("Cast",
                                               bias_rounded_node.output, [quantized_bias_name],
                                               quantized_bias_name + "_node",
                                               to=qType)
        new_node_list.append(bias_cast_node)

        return

    def quantize_bias(self, node, new_node_list):
        '''
        Quantized the bias. Zero Point == 0 and Scale == Input_Scale * Weight_Scale
        '''

        # get scale for weight
        weight_scale_name = self.quantized_value_map[node.input[1]].scale_name
        weight_initializer = find_by_name(weight_scale_name, self.model.graph.initializer)
        weight_scale = self.find_weight_data(weight_initializer)

        # get bias
        bias_name = node.input[2]
        bias_initializer = find_by_name(bias_name, self.model.graph.initializer)
        bias_data = self.find_weight_data(bias_initializer)
        quantized_bias_name = bias_name + "_quantized"

        # input scale is not provided and this input is dynamically quantized so it is not pre-computed at this point
        # so resort to dynamic quantization for bias
        if self.quantization_params is None or node.input[0] not in self.quantization_params and node.input[
                0] not in self.quantized_value_map:
            self._dynamic_quantize_bias(node.input[0], weight_scale_name, bias_name, quantized_bias_name, new_node_list)
        else:
            # get scale for input
            if node.input[0] in self.quantized_value_map:
                input_scale_name = self.quantized_value_map[node.input[0]].scale_name
            elif node.input[0] in self.quantization_params:
                _, input_scale_name, _, _, _ = self.get_quantization_params(node.input[0])
            else:
                raise ValueError("Expected {} to be in quantized value map for static quantization".format(
                    node.input[0]))

            inputscale_initializer = find_by_name(input_scale_name, self.model.graph.initializer)
            input_scale = self.find_weight_data(inputscale_initializer)

            # calcuate scale for bias

            bias_scale = input_scale * weight_scale

            # quantize bias
            quantized_data = (np.asarray(bias_data) / bias_scale).round().astype(np.int32)

            # update bias initializer
            bias_np_data = np.asarray(quantized_data, dtype=np.int32).reshape(bias_initializer.dims)
            packed_bias_initializer = onnx.numpy_helper.from_array(bias_np_data, quantized_bias_name)
            self.model.graph.initializer.extend([packed_bias_initializer])

            # log entries for this quantized bias value
            quantized_bias_entry = QuantizedInitializer(bias_name,
                                                        bias_initializer, [0], [0], [0], [bias_scale],
                                                        bias_data,
                                                        quantized_data,
                                                        qType=onnx_proto.TensorProto.INT32)
            self._quantized_weights.append(quantized_bias_entry)

            assert (bias_name not in self.quantized_value_map)
            quantized_value = QuantizedValue(bias_name, quantized_bias_name, "", "", QuantizedValueType.Initializer,
                                             None, onnx_proto.TensorProto.INT32)
            self.quantized_value_map[bias_name] = quantized_value

        return quantized_bias_name

    def quantize_inputs(self, node, indices):
        '''
        Given a node, this function quantizes the inputs as follows:
            - If input is a initializer, quantize the initializer data, replace old initializer
              with new initializer
            - Else, add QuantizeLinear nodes to perform quantization

            parameter node: node being quantized in NodeProto format.
            parameter indices: input indices to quantize.
            return: (List of quantized input names,
                     List of zero point names used for input quantization,
                     List of scale names used for input quantization,
                     List of new QuantizeLinear nodes created)
        '''

        quantized_input_names = []
        zero_point_names = []
        scale_names = []
        nodes = []

        for input_index in indices:
            node_input = node.input[input_index]

            # Find if this input is already quantized
            if node_input in self.quantized_value_map:
                quantized_value = self.quantized_value_map[node_input]
                qType = self.weight_qType if quantized_value.value_type == QuantizedValueType.Initializer else self.input_qType
                if quantized_value.qType != qType:
                    raise ValueError(
                        "{} is being used by multiple nodes which are being quantized to different types. "
                        "This is not suported.", node_input)

                quantized_input_names.append(quantized_value.q_name)
                scale_names.append(quantized_value.scale_name)
                zero_point_names.append(quantized_value.zp_name)
                continue

            # Quantize the input
            initializer = find_by_name(node_input, self.model.graph.initializer)
            if initializer is not None:
                if node.op_type == "Conv":
                    weight = self._get_quantized_weight_convolution(initializer, self.weight_qType)
                else:
                    weight = self._get_quantized_weight(initializer, self.weight_qType)

                # Update graph
                self._update_graph(weight)

                quantized_input_names.append(weight.name + "_quantized")
                zero_point_names.append(weight.name + "_zero_point")
                scale_names.append(weight.name + "_scale")
            else:
                # Add QuantizeLinear node.
                qlinear_node = find_by_name(node_input + "_QuantizeLinear", self.nodes)
                if qlinear_node is None:
                    quantize_input_nodes = self._get_quantize_input_nodes(node, input_index, self.input_qType)
                    nodes.extend(quantize_input_nodes)
                    qlinear_node = quantize_input_nodes[-1]

                if qlinear_node.op_type == "QuantizeLinear":
                    quantized_input_names.extend(qlinear_node.output)
                    scale_names.append(qlinear_node.input[1])
                    zero_point_names.append(qlinear_node.input[2])
                else:
                    quantized_input_names.append(qlinear_node.output[0])
                    scale_names.append(qlinear_node.output[1])
                    zero_point_names.append(qlinear_node.output[2])

        return (quantized_input_names, zero_point_names, scale_names, nodes)

    def dequantize_value(self, value_name):
        '''
        Given a value (input/output) which is quantized, add a DequantizeLinear node to dequantize
        it back to float32

            parameter value_name: value to dequantize
            parameter new_nodes_list: List of new nodes created before processing current node
            return: None if there is already a DequantizeLinear node that dequantizes it
                    A DequantizeLinear node otherwise
        '''
        if value_name in self.quantized_value_map:
            quantized_value = self.quantized_value_map[value_name]
            # Add DequantizeLinear Node for this input
            dqlinear_name = value_name + "_DequantizeLinear"
            dqlinear_node = find_by_name(dqlinear_name, self.nodes)
            if dqlinear_node is None:
                dqlinear_inputs = [quantized_value.q_name, quantized_value.scale_name, quantized_value.zp_name]
                dequantize_node = onnx.helper.make_node("DequantizeLinear", dqlinear_inputs, [value_name],
                                                        dqlinear_name)
                return dequantize_node
            else:
                # DQ op is already present, assert it's output matches the input of current node
                assert (value_name == dqlinear_node.output[0])
        return None

    def _dequantize_outputs(self):
        '''
        Dequantize output if it is quantized

            parameter new_nodes_list: List of new nodes created before processing current node
            return: List of new nodes created
        '''
        for output in self.model.graph.output:
            dequantize_node = self.dequantize_value(output.name)
            if dequantize_node is not None:
                self.nodes.append(dequantize_node)


def check_opset_version(org_model, force_fusions):
    '''
        Check opset version of original model and set opset version and fuse_dynamic_quant accordingly.
        If opset version < 10, set quantized model opset version to 10.
        If opset version == 10, do quantization without using dynamicQuantizeLinear operator.
        If opset version == 11, do quantization using dynamicQuantizeLinear operator.

        :return: fuse_dynamic_quant boolean value.
    '''
    global onnx_op_set_version
    opset_version = org_model.opset_import[0].version
    fuse_dynamic_quant = False

    if opset_version < 11 and force_fusions == True:
        print("Warning: The original model opset version is {}, which does not support node fusions.\n\
            Forcing fusions can break other nodes in the model.".format(opset_version))
        onnx_op_set_version = 11
        fuse_dynamic_quant = True
        return fuse_dynamic_quant

    if opset_version < 10:
        print("Warning: The original model opset version is {}, which does not support quantized operators.\n\
            The opset version of quantized model will be set to 10. Use onnx model checker to verify model after quantization."
              .format(opset_version))
        onnx_op_set_version = 10
    elif opset_version == 10:
        onnx_op_set_version = 10
    else:
        fuse_dynamic_quant = True
    return fuse_dynamic_quant


def quantize(model,
             per_channel=False,
             nbits=8,
             quantization_mode=QuantizationMode.IntegerOps,
             static=False,
             force_fusions=False,
             symmetric_activation=False,
             symmetric_weight=False,
             quantization_params=None,
             nodes_to_quantize=None,
             nodes_to_exclude=None):
    '''
        Given an onnx model, create a quantized onnx model and save it into a file

    :param model: ModelProto to quantize
    :param per_channel: quantize weights per channel
    :param nbits: number of bits to represent quantized data. Currently only supporting 8-bit types
    :param quantization_mode: Can be one of the QuantizationMode types.
        IntegerOps:
            the function will use integer ops. Only ConvInteger and MatMulInteger ops are supported now.
        QLinearOps:
            the function will use QLinear ops. Only QLinearConv and QLinearMatMul ops are supported now.
    :param static:
        True: The inputs/activations are quantized using static scale and zero point values
              specified through quantization_params.
        False: The inputs/activations are quantized using dynamic scale and zero point values
               computed while running the model.
    :param force_fusions:
        True: Fuses nodes added for dynamic quantization
        False: No fusion is applied for nodes which are added for dynamic quantization.
        Should be only used in cases where backends want to apply special fusion routines
    :param symmetric_activation:
        True: activations are quantized into signed integers.
        False: activations are quantized into unsigned integers.
    :param symmetric_weight:
        True: weights are quantized into signed integers.
        False: weights are quantized into unsigned integers.
    :param quantization_params:
        Dictionary to specify the zero point and scale values for inputs to conv and matmul nodes.
        Should be specified when static is set to True.
        The quantization_params should be specified in the following format:
            {
                "input_name": [zero_point, scale]
            }.
        zero_point should be of type np.uint8 and scale should be of type np.float32.
        example:
            {
                'resnet_model/Relu_1:0': [np.uint8(0), np.float32(0.019539741799235344)],
                'resnet_model/Relu_2:0': [np.uint8(0), np.float32(0.011359662748873234)]
            }
    :return: ModelProto with quantization
    :param nodes_to_quantize:
        List of nodes names to quantize. When this list is not None only the nodes in this list
        are quantized.
        example:
        [
            'Conv__224',
            'Conv__252'
        ]
    :param nodes_to_exclude:
        List of nodes names to exclude. The nodes in this list will be excluded from quantization
        when it is not None.
    '''
    if nbits == 8:
        input_qType = onnx_proto.TensorProto.INT8 if symmetric_activation else onnx_proto.TensorProto.UINT8
        weight_qType = onnx_proto.TensorProto.INT8 if symmetric_weight else onnx_proto.TensorProto.UINT8
        mode = quantization_mode
        copy_model = onnx_proto.ModelProto()
        copy_model.CopyFrom(model)
        fuse_dynamic_quant = check_opset_version(copy_model, force_fusions)
        quantizer = ONNXQuantizer(copy_model, per_channel, mode, static, fuse_dynamic_quant, weight_qType, input_qType,
                                  quantization_params, nodes_to_quantize, nodes_to_exclude)
        quantizer.quantize_model()
        quantizer.model.producer_name = __producer__
        quantizer.model.producer_version = __version__
        return quantizer.model
    else:
        raise ValueError('Only 8 bit quantization is currently supported')
