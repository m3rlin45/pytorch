from typing import Any, Callable

import torch
import torch.distributed as dist


def _allreduce_fut(
    process_group: dist.ProcessGroup, tensor: torch.Tensor
) -> torch.futures.Future:
    "Averages the input gradient tensor by allreduce and returns a future."
    group_to_use = process_group if process_group is not None else dist.group.WORLD

    # Apply the division first to avoid overflow, especially for FP16.
    tensor.div_(group_to_use.size())

    return dist.all_reduce(tensor, group=group_to_use, async_op=True).get_future()


def allreduce_hook(
    process_group: dist.ProcessGroup, bucket: dist.GradBucket
) -> torch.futures.Future:
    """
    This DDP communication hook just calls ``allreduce`` using ``GradBucket``
    tensors. Once gradient tensors are aggregated across all workers, its ``then``
    callback takes the mean and returns the result. If user registers this hook,
    DDP results is expected to be same as the case where no hook was registered.
    Hence, this won't change behavior of DDP and user can use this as a reference
    or modify this hook to log useful information or any other purposes while
    unaffecting DDP behavior.

    Example::
        >>> ddp_model.register_comm_hook(process_group, allreduce_hook)
    """
    return _allreduce_fut(process_group, bucket.get_tensor())


def fp16_compress_hook(
    process_group: dist.ProcessGroup, bucket: dist.GradBucket
) -> torch.futures.Future:
    """
    This DDP communication hook implements a simple gradient compression
    approach that casts ``GradBucket`` tensors to half-precision floating-point format (``torch.float16``)
    and then divides it by the process group size.
    It allreduces those ``float16`` gradient tensors. Once compressed gradient
    tensors are allreduced, the chained callback ``decompress`` casts it back to the input data type (such as ``float32``).

    Example::
        >>> ddp_model.register_comm_hook(process_group, fp16_compress_hook)
    """
    group_to_use = process_group if process_group is not None else dist.group.WORLD
    world_size = group_to_use.size()

    compressed_tensor = bucket.get_tensor().to(torch.float16).div_(world_size)

    fut = dist.all_reduce(
        compressed_tensor, group=group_to_use, async_op=True
    ).get_future()

    def decompress(fut):
        decompressed_tensor = bucket.get_tensor()
        # Decompress in place to reduce the peak memory.
        # See: https://github.com/pytorch/pytorch/issues/45968
        decompressed_tensor.copy_(fut.value()[0])
        return decompressed_tensor

    return fut.then(decompress)


class OptimizerHookState(object):
    """
    Holds state for running optimizer in-line after DDP communication hook.
    Currently contains optimizer class as well as optimizer args.
    """
    __slots__ = ['functional_optim_cls', 'functional_optim_args', 'functional_optimizer']

    def __init__(self, functional_optim_cls, *functional_optim_args):
        # TODO: Support kwargs
        self.functional_optim_cls = functional_optim_cls
        self.functional_optim_args = functional_optim_args
        self.functional_optimizer = self.functional_optim_cls(
            [],
            *self.functional_optim_args,
            allow_empty_param_list=True
        )
        if not hasattr(self.functional_optimizer, 'step_param'):
            raise ValueError(
                f"Class {self.functional_optim_cls} must implement method step_param."
            )

def hook_then_optimizer(
    hook: Callable[[Any, dist.GradBucket], torch.futures.Future], optimizer_state: OptimizerHookState
) -> Callable[[Any, dist.GradBucket], torch.futures.Future]:
    """Runs optimizer in a functional fashion after DDP communication hook."""

    def hook_then_optimizer_wrapper(
        hook_state, bucket: dist.GradBucket
    ) -> torch.futures.Future:
        # Run original hook
        fut = hook(hook_state, bucket)

        def optimizer_step(fut):
            gradient_tensors = bucket.get_per_parameter_tensors()
            model_params = bucket.get_model_params_for_bucket()
            for grad_tensor, model_param in zip(gradient_tensors, model_params):
                optimizer_state.functional_optimizer.step_param(
                    model_param,
                    grad_tensor,
                )
            return bucket.get_tensor()
        return fut.then(optimizer_step)

    return hook_then_optimizer_wrapper

def fp16_compress_wrapper(
    hook: Callable[[Any, dist.GradBucket], torch.futures.Future]
) -> Callable[[Any, dist.GradBucket], torch.futures.Future]:
    """
    This wrapper casts the input gradient tensors of a given DDP communication hook to half-precision
    floating point format (``torch.float16``), and casts the resulting tensors of the given hook back to
    the input data type, such as ``float32``.

    Therefore, ``fp16_compress_hook`` is equivalent to ``fp16_compress_wrapper(allreduce_hook)``.

    Example::
        >>> state = PowerSGDState(process_group=process_group, matrix_approximation_rank=1, start_powerSGD_iter=10)
        >>> ddp_model.register_comm_hook(state, fp16_compress_wrapper(powerSGD_hook))
    """

    def fp16_compress_wrapper_hook(
        hook_state, bucket: dist.GradBucket
    ) -> torch.futures.Future:
        # Cast bucket tensor to the FP16.
        bucket.set_tensor(bucket.get_tensor().to(torch.float16))

        fut = hook(hook_state, bucket)

        def decompress(fut):
            decompressed_tensor = bucket.get_tensor()
            # Decompress in place to reduce the peak memory.
            # See: https://github.com/pytorch/pytorch/issues/45968
            decompressed_tensor.copy_(fut.value()[0])
            return decompressed_tensor

        # Decompress after hook has run.
        return fut.then(decompress)

    return fp16_compress_wrapper_hook
