# -*- coding:utf8 -*-
r"""Graph Visualization Transformer

Transformers that export a flatbuffer file presenting the Tensorflow Lite's model.
"""
import re
from collections import OrderedDict
import numpy as np
from copy import deepcopy

from utensor_cgen.ir import OperationInfo, uTensorGraph
import flatbuffers
import utensor_cgen.third_party.tflite as tflite
from utensor_cgen.third_party.tflite.BuiltinOperator import BuiltinOperator
from utensor_cgen.third_party.tflite.BuiltinOptions import BuiltinOptions
from utensor_cgen.third_party.tflite.TensorType import TensorType
from utensor_cgen.logger import logger
from utensor_cgen.utils import parse_tensor_name

from .base import Transformer

__all__ = ["TFLiteExporter"]

class FlatbufferOpManager:
    op_list = list()
    code_name_lookup = {v: k for k, v in BuiltinOperator.__dict__.items()}
    
    def regsiter_op(self, op_type_str):
        if op_type_str not in BuiltinOperator.__dict__:
            assert("invalid op str")
        if op_type_str not in self.op_list:
            self.op_list.append(op_type_str)
        return self.op_index(op_type_str)
    
    def op_index(self, op_type_str):
        return self.op_list.index(op_type_str)
    def op_code(self, op_type_str):
        return BuiltinOperator.__dict__[op_type_str]
    def code2name(self, _op_code):
        return self.code_name_lookup[_op_code]
    def code2index(self, _op_code):
        return self.op_list.index(self.code2name(_op_code))
    def index2code(self, _index):
        op_type = self.op_list[_index]
        return  BuiltinOperator.__dict__[op_type]
    def index2name(self, _index):
        code = self.index2code(_index)
        return self.code2name(code)

class TFLiteExporter(Transformer):
  METHOD_NAME = 'tflite_export'
  KWARGS_NAMESCOPE = '_tflite_export'
  __Max_fbuff_size = 1024 * 10
  __op_manager = FlatbufferOpManager()
  fbuilder = flatbuffers.Builder(__Max_fbuff_size)
  __tensor_buffer_index = OrderedDict()   # out_name : data buff vec
                                          # added to the Model object
  __tensor_index = OrderedDict()          # out_name : ref tensor object
                                          #added to the Subgraph object
  
  op_exec_order = list()

  def __init__(self):
    self.prune_graph = False #what is this?

  def transform(self, ugraph):
    #create tensor data buffer
    #create const tensors
    #update tensor_index
    self.__create_static_tensor(ugraph)

    #create intermediate tensors
    #update tensor_index
    self.__create_variable_tensors(ugraph)

    #interacts with FlatbufferOpManager
    op_codes_vec = self.__create_op_codes(ugraph) #to be added into the Model

    #create subgraph
    #first, create the tensor vector
    tflite.SubGraph.SubGraphStartTensorsVector(self.fbuilder, len(self.__tensor_index))
    for t_name, f_obj in self.__tensor_index.items():
      self.fbuilder.PrependUOffsetTRelative(f_obj)
    tensors_vec = self.fbuilder.EndVector(len(self.__tensor_index))

    ## TODO: traverse the graph and produce
    ops_vec = None
    ## here

    tflite.SubGraph.SubGraphStart(self.fbuilder)
    tflite.SubGraph.SubGraphAddTensors(self.fbuilder, tensors_vec)
    tflite.SubGraph.SubGraphAddOperators(self.fbuilder, ops_vec)
    subgraph = tflite.SubGraph.SubGraphEnd(self.fbuilder)

    tflite.Model.ModelStartSubgraphsVector(self.fbuilder, 1) #TFLM runtime only support 1 subgraph
    self.fbuilder.PrependUOffsetTRelative(subgraph)
    subgraph_vec = self.fbuilder.EndVector(1)

    #model
    # first, add tensor buffer here
    tflite.Model.ModelStartBuffersVector(self.fbuilder, len(self.__tensor_buffer_index))
    for t_name, f_obj in self.__tensor_buffer_index.items():
      self.fbuilder.PrependUOffsetTRelative(f_obj)
    buff_vec = self.fbuilder.EndVector(len(self.__tensor_buffer_index))

    tflite.Model.ModelStart(self.fbuilder)
    tflite.Model.ModelAddVersion(self.fbuilder, 100) #TODO: change this
    tflite.Model.ModelAddOperatorCodes(self.fbuilder, op_codes_vec)
    tflite.Model.ModelAddSubgraphs(self.fbuilder, subgraph_vec)
    tflite.Model.ModelAddBuffers(self.fbuilder, buff_vec)

    model = tflite.Model.ModelEnd(self.fbuilder)

    self.fbuilder.Finish(model)

    output = self.fbuilder.Output() #do something with the output here
    print(output)

    return ugraph

  def __create_static_tensor(self, ugraph):
    target_ops = ["Inline", "Const"]
    export_tensor_name = True
    for op_name in ugraph.topo_order:
      op_info = ugraph.ops_info[op_name]
      op_type = op_info.op_type
      if op_type in target_ops:  #TODO: check if op data is empty
        out_tensor_info = op_info.output_tensors[0]
        out_tname, out_dtype, tensor_shape = (out_tensor_info.name,
                        out_tensor_info.dtype,
                        out_tensor_info.shape)
        #weight
        value = op_info.op_attr['value'].value.np_array.flatten() #TODO: specify endianness here
        raw_data = value.tobytes()
        tflite.Buffer.BufferStartDataVector(self.fbuilder, len(raw_data))
        for i in raw_data:
          self.fbuilder.PrependUint8(i)
        data_vec = self.fbuilder.EndVector(len(raw_data))
        self.__tensor_buffer_index[out_tname] = data_vec

        #shape
        tflite.Tensor.TensorStartShapeVector(self.fbuilder, len(tensor_shape))
        for d in tensor_shape:
          self.fbuilder.PrependInt32(d)
        shape_vec = self.fbuilder.EndVector(len(tensor_shape))

        #name
        if export_tensor_name:
          tensor_name = self.fbuilder.CreateString(self.__legalize_name(out_tname))

        #quantization
        ## only per-layer supported
        (q_scale, q_zero) = self.__sym_quantization_info(op_info)
        q_param = self.__create_quantization_param([q_scale], [q_zero])

        #tensor object
        tflite.Tensor.TensorStart(self.fbuilder)
        tflite.Tensor.TensorAddShape(self.fbuilder, shape_vec)
        tflite.Tensor.TensorAddType(self.fbuilder, TensorType.INT8) #TODO: a conversion class here, out_dtype
        if export_tensor_name:
          tflite.Tensor.TensorAddName(self.fbuilder, tensor_name)
        tflite.Tensor.TensorAddQuantization(self.fbuilder, q_param)
        tflite.Tensor.TensorAddIsVariable(self.fbuilder, False)

        tflite.Tensor.TensorAddBuffer(self.fbuilder, 0)
        new_tensor = tflite.Tensor.TensorEnd(self.fbuilder)
        self.__tensor_index[out_tname] = new_tensor

  def __output_vector(self, tensor_infos):
    n = len(tensor_infos)
    tflite.Operator.OperatorStartOutputsVector(self.fbuilder, n)
    for tensor_info in tensor_infos:
      tensor_index = self.__tensor_index.keys().index(tensor_info.name)
      self.fbuilder.PrependInt32(tensor_index)
    return self.fbuilder.EndVector(n)

  def __input_vector(self, tensor_infos):
    n = len(tensor_infos)
    tflite.Operator.OperatorStartInputsVector(self.fbuilder, n)
    for tensor_info in tensor_infos:
      tensor_index = self.__tensor_index.keys().index(tensor_info.name)
      self.fbuilder.PrependInt32(tensor_index)
    return self.fbuilder.EndVector(n)

  def __create_op_codes(self, ugraph):
    #scan, translation and register
    op_codes = list()
    for op_name in ugraph.topo_order:
      op_type = ugraph.ops_info[op_name].op_type
      tflite.OperatorCode.OperatorCodeStart(self.fbuilder)
      #TODO: op type translation happen here
      opcode_index = self.__op_manager.regsiter_op(op_type)
      tflite.OperatorCode.OperatorCodeAddBuiltinCode(self.fbuilder, opcode_index)
      op_codes.append(tflite.OperatorCode.OperatorCodeEnd(self.fbuilder))

    tflite.Model.ModelStartOperatorCodesVector(self.fbuilder, len(op_codes))
    for it_op_code in op_codes:
        self.fbuilder.PrependUOffsetTRelative(it_op_code)
    op_code_vec = self.fbuilder.EndVector(len(op_codes))
    return op_code_vec #needs to be added into the Model

  def add_FullyConnectedOp(self, inputs, outputs):
    op_inputs = self.__input_vector(inputs)
    op_outputs = self.__output_vector(outputs)

    tflite.Operator.OperatorStart(self.fbuilder)
    tflite.Operator.OperatorAddOpcodeIndex(self.fbuilder, self.__op_manager.op_index('FULLY_CONNECTED'))
    tflite.Operator.OperatorAddInputs(self.fbuilder, op_inputs)
    tflite.Operator.OperatorAddOutputs(self.fbuilder, op_outputs)
    op = tflite.Operator.OperatorEnd(self.fbuilder)
    self.op_exec_order.append(op)


  def __sym_quantization_info(self, op_info):
    """
    https://www.tensorflow.org/lite/performance/quantization_spec
    C++: TfLiteAffineQuantization struct
    zero-point = 0 : symmetric quantization
    Returns (scale, zero)

    TODO: per-axis quantization
    """

    values = op_info.op_attr['value'].value.np_array.flatten()
    abs_max = np.absolute(values).max()
    #based on quantizted int8 dtype
    scale = 127 / abs_max
   
    return (scale, 0)

  def __create_quantization_param(self, scales, zeros):
    assert len(scales) != len(zeros), "scales and zero-points length mismatch"
    

    tflite.QuantizationParameters.QuantizationParametersStartScaleVector(self.fbuilder, len(scales))
    for _scale in scales:
      self.fbuilder.PrependFloat32(_scale)
    q_scales_vec = self.fbuilder.EndVector(len(scales))

    tflite.QuantizationParameters.QuantizationParametersStartZeroPointVector(self.fbuilder, len(zeros))
    for _zero in zeros:
      self.fbuilder.PrependFloat32(_zero)
    q_zeros_vec = self.fbuilder.EndVector(len(zeros))
  
    tflite.QuantizationParameters.QuantizationParametersStart(self.fbuilder)
    tflite.QuantizationParameters.QuantizationParametersAddScale(self.fbuilder, q_scales_vec)
    tflite.QuantizationParameters.QuantizationParametersAddZeroPoint(self.fbuilder, q_zeros_vec)
    q_param = tflite.QuantizationParameters.QuantizationParametersEnd(self.fbuilder)

    return q_param

  # These are often the intermediate output tensors with online quantization
  def __create_variable_tensors(self, ugraph):
    tensor_infos = set()
    for op_name in ugraph.topo_order:
      tensor_infos.update(ugraph.ops_info[op_name].inputs)
      tensor_infos.update(ugraph.ops_info[op_name].outputs)
    for tensor_info in tensor_infos:
      tflite.Tensor.TensorStartShapeVector(self.fbuilder, len(tensor_info.shape))
      for d in tensor_info.shape:
        self.fbuilder.PrependInt32(d)
      shape_vec = self.fbuilder.EndVector(len(tensor_info.shape))

      tensor_name = self.fbuilder.CreateString(self.__legalize_name(tensor_info.name))

      q_param = self.__create_quantization_param([-20],[200]) #TODO: workout how to treat the quantization here

      tflite.Tensor.TensorStart(self.fbuilder)
      tflite.Tensor.TensorAddShape(self.fbuilder, shape_vec)
      tflite.Tensor.TensorAddType(self.fbuilder, TensorType.INT8) #TODO: tensor type conversion here
      tflite.Tensor.TensorAddName(self.fbuilder, tensor_name)
      tflite.Tensor.TensorAddQuantization(self.fbuilder, q_param)
      tflite.Tensor.TensorAddIsVariable(self.fbuilder, True)

      self.__tensor_index[tensor_info.name] = tflite.Tensor.TensorEnd(self.fbuilder)

  def __legalize_name(self, str):
    #TODO: legalize the name
    return str

# How to construct the quantization parameter for intermediate tensors?
## zero point and scale
