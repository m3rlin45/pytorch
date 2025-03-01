import copy
import warnings
from typing import List, NamedTuple, Iterable, Any, Optional

import torch
import torch.fx
import tensorrt as trt
from torch.fx.experimental.normalize import NormalizeArgs


# Borrowed from torch2trt
def torch_dtype_to_trt(dtype):
    if trt.__version__ >= '7.0' and dtype == torch.bool:
        return trt.bool
    elif dtype == torch.int8:
        return trt.int8
    elif dtype == torch.int32:
        return trt.int32
    elif dtype == torch.float16:
        return trt.float16
    elif dtype == torch.float32:
        return trt.float32
    else:
        raise TypeError("%s is not supported by tensorrt" % dtype)


def torch_dtype_from_trt(dtype):
    if dtype == trt.int8:
        return torch.int8
    elif trt.__version__ >= '7.0' and dtype == trt.bool:
        return torch.bool
    elif dtype == trt.int32:
        return torch.int32
    elif dtype == trt.float16:
        return torch.float16
    elif dtype == trt.float32:
        return torch.float32
    else:
        raise TypeError("%s is not supported by torch" % dtype)

def torch_device_to_trt(device):
    if device.type == torch.device("cuda").type:
        return trt.TensorLocation.DEVICE
    elif device.type == torch.device("cpu").type:
        return trt.TensorLocation.HOST
    else:
        return TypeError("%s is not supported by tensorrt" % device)


def torch_device_from_trt(device):
    if device == trt.TensorLocation.DEVICE:
        return torch.device("cuda")
    elif device == trt.TensorLocation.HOST:
        return torch.device("cpu")
    else:
        return TypeError("%s is not supported by torch" % device)


class TRTModule(torch.nn.Module):
    def __init__(self, engine=None, input_names=None, output_names=None, fp16_output=False):
        super(TRTModule, self).__init__()
        self._register_state_dict_hook(TRTModule._on_state_dict)
        self.engine = engine
        if self.engine is not None:
            self.context = self.engine.create_execution_context()
        self.input_names = input_names
        self.output_names = output_names

        # Indicate output is in fp16
        self.fp16_output = fp16_output

    def _on_state_dict(self, state_dict, prefix, local_metadata):
        state_dict[prefix + "engine"] = bytearray(self.engine.serialize())
        state_dict[prefix + "input_names"] = self.input_names
        state_dict[prefix + "output_names"] = self.output_names
        state_dict[prefix + "fp16_output"] = self.fp16_output

    def _load_from_state_dict(
        self,
        state_dict,
        prefix,
        local_metadata,
        strict,
        missing_keys,
        unexpected_keys,
        error_msgs,
    ):
        engine_bytes = state_dict[prefix + "engine"]

        with trt.Logger() as logger, trt.Runtime(logger) as runtime:
            self.engine = runtime.deserialize_cuda_engine(engine_bytes)
            self.context = self.engine.create_execution_context()

        self.input_names = state_dict[prefix + "input_names"]
        self.output_names = state_dict[prefix + "output_names"]

    def forward(self, *inputs):
        batch_size = inputs[0].shape[0]
        contiguous_inputs: List[torch.Tensor] = [i.contiguous() for i in inputs]
        bindings: List[Any] = [None] * (len(self.input_names) + len(self.output_names))

        # create output tensors
        outputs: List[torch.Tensor] = []
        for i, output_name in enumerate(self.output_names):
            idx: int = self.engine.get_binding_index(output_name)
            dtype = torch_dtype_from_trt(self.engine.get_binding_dtype(idx))

            if self.engine.has_implicit_batch_dimension:
                shape = (batch_size,) + tuple(self.engine.get_binding_shape(idx))
            else:
                shape = tuple(self.engine.get_binding_shape(idx))

            device = torch_device_from_trt(self.engine.get_location(idx))
            output = torch.empty(size=shape, dtype=dtype, device=device)
            outputs.append(output)
            bindings[idx] = output.data_ptr()

        for i, input_name in enumerate(self.input_names):
            idx = self.engine.get_binding_index(input_name)
            bindings[idx] = contiguous_inputs[i].data_ptr()

        self.context.execute_async(
            batch_size, bindings, torch.cuda.current_stream().cuda_stream
        )

        if len(outputs) == 1:
            return outputs[0]

        return tuple(outputs)

    def enable_profiling(self):
        if not self.context.profiler:
            self.context.profiler = trt.Profiler()


CONVERTERS = {}


def tensorrt_converter(key):
    def register_converter(converter):
        CONVERTERS[key] = converter
        return converter
    return register_converter


class InputTensorSpec(NamedTuple):
    shape : torch.Size
    dtype : torch.dtype
    device : torch.device = torch.device("cpu")
    has_batch_dim : bool = True

    @classmethod
    def from_tensor(cls, tensor: torch.Tensor):
        return cls(tensor.shape, tensor.dtype, tensor.device)

    @classmethod
    def from_tensors(cls, tensors: Iterable[torch.Tensor]):
        return [cls.from_tensor(t) for t in tensors]


class BaseTRTInterpreter(torch.fx.Interpreter):
    def __init__(
        self,
        module : torch.fx.GraphModule,
        input_specs : List[InputTensorSpec],
        explicit_batch_dimension : bool = False,
        logger_level=trt.Logger.WARNING
    ):
        super().__init__(module)

        self.logger = trt.Logger(logger_level)
        self.builder = trt.Builder(self.logger)

        if explicit_batch_dimension:
            EXPLICIT_BATCH = 1 << (int)(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)
            self.network = self.builder.create_network(EXPLICIT_BATCH)
        else:
            self.network = self.builder.create_network()

        self.input_specs_iter = iter(input_specs)
        self._cur_node_name: Optional[str] = None
        self._input_names: List[str] = []
        self._output_names: List[str] = []

    def run(
        self,
        max_batch_size=64,
        max_workspace_size=1 << 25,
        fp16_mode=True,
        int8_mode=False,
        strict_type_constraints=True
    ):
        # TODO hack, should check contents of args and remove fp16_mode probably
        self.fp16_mode = fp16_mode

        if int8_mode and not self.builder.platform_has_fast_int8:
            warnings.warn("Current platform doesn't support fast native int8!")

        if fp16_mode and not self.builder.platform_has_fast_fp16:
            warnings.warn("Current platform doesn't support fast native fp16!")

        super().run()

        self.builder.max_batch_size = max_batch_size
        builder_config = self.builder.create_builder_config()
        builder_config.max_workspace_size = max_workspace_size
        if fp16_mode:
            builder_config.set_flag(trt.BuilderFlag.FP16)

        if int8_mode:
            builder_config.set_flag(trt.BuilderFlag.INT8)

        if strict_type_constraints:
            builder_config.set_flag(trt.BuilderFlag.STRICT_TYPES)

        engine = self.builder.build_engine(self.network, builder_config)
        assert(engine)
        return engine, self._input_names, self._output_names

    def run_node(self, n):
        self._cur_node_name = str(n)
        return super().run_node(n)

    def placeholder(self, target, args, kwargs):
        self._input_names.append(target)
        shape, dtype, _, has_batch_dim = next(self.input_specs_iter)
        if self.network.has_implicit_batch_dimension:
            if has_batch_dim:
                shape = shape[1:]
        else:
            assert has_batch_dim, "It's required to specify batch dimension when it's explicit in TensorRT network."
        return self.network.add_input(name=target, shape=tuple(shape), dtype=torch_dtype_to_trt(dtype))

    def call_module(self, target, args, kwargs):
        assert isinstance(target, str)
        submod = self.fetch_attr(target)
        converter = CONVERTERS.get(type(submod))

        if not converter:
            raise RuntimeError(f'Conversion of module of type {type(submod)} not currently supported!')

        return converter(self.network, submod, args, kwargs, self._cur_node_name)

    def call_function(self, target, args, kwargs):
        converter = CONVERTERS.get(target)

        if not converter:
            raise RuntimeError(f'Conversion of function {torch.typename(target)} not currently supported!')

        return converter(self.network, target, args, kwargs, self._cur_node_name)

    def call_method(self, target, args, kwargs):
        assert isinstance(target, str)
        converter = CONVERTERS.get(target)

        if not converter:
            raise RuntimeError(f'Conversion of method {target} not currently supported!')

        return converter(self.network, target, args, kwargs, self._cur_node_name)

    def output(self, target, args, kwargs):
        assert len(args) == 1
        outputs = args[0] if isinstance(args[0], tuple) else (args[0],)

        if not all(isinstance(output, trt.tensorrt.ITensor) for output in outputs):
            raise RuntimeError('TensorRT requires all outputs to be Tensor!')

        for i, output in enumerate(outputs):
            name = f'output{i}'
            output.name = name
            self.network.mark_output(output)
            if self.fp16_mode:
                output.dtype = trt.float16
            else:
                output.dtype = trt.float32
            self._output_names.append(name)


class TRTInterpreter(BaseTRTInterpreter):
    """
    Use this for general case where there're PyTorch vanilla ops in the FX mdoule.
    """
    def __init__(self, module : torch.nn.Module, input_specs : List[InputTensorSpec], logger_level=trt.Logger.WARNING):
        # Preprocess the model
        if not isinstance(module, torch.fx.GraphModule):
            module = torch.fx.symbolic_trace(module)
        else:
            module = copy.deepcopy(module)
        module = module.cpu().float()
        module = NormalizeArgs(module).transform()
        super().__init__(module, input_specs, logger_level)
