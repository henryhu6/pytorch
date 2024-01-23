from typing import Any, cast, Optional, Union

import typing_extensions

import torch.nn as nn
from torch._prims_common import DeviceLikeType

from torch.distributed._composable import contract
from torch.distributed._tensor import DeviceMesh

from ._fsdp_common import FSDPMeshInfo, HSDPMeshInfo
from ._fsdp_init import (
    _get_managed_modules,
    _get_managed_states,
    _get_post_forward_mesh_info,
    _init_default_fully_shard_mesh,
    _move_states_to_device,
    _normalize_device,
)
from ._fsdp_param_group import FSDPParamGroup
from ._fsdp_state import _get_module_fsdp_state, FSDPState


# The decorator adds a state object to `module` that can be accessed via
# `fully_shard.state(module)`. The state object and module are 1:1.
@contract(state_cls=FSDPState)
def fully_shard(
    module: nn.Module,
    *,
    mesh: Optional[DeviceMesh] = None,
    device: DeviceLikeType = "cuda",
    reshard_after_forward: Union[bool, int] = True,
):
    if isinstance(module, (nn.ModuleList, nn.ModuleDict)):
        raise ValueError(
            f"fully_shard does not support containers that do not implement forward: {module}"
        )
    device = _normalize_device(device)
    mesh = mesh or _init_default_fully_shard_mesh(device.type)
    if mesh.ndim not in (1, 2):
        raise ValueError(f"fully_shard expects a 1D or 2D DeviceMesh but got {mesh}")
    elif mesh.ndim == 1:
        mesh_info = FSDPMeshInfo(mesh, shard_mesh_dim=0)
    elif mesh.ndim == 2:
        mesh_info = HSDPMeshInfo(mesh, shard_mesh_dim=1, replicate_mesh_dim=0)
    if device.type != "meta" and device.type != mesh.device_type:
        raise ValueError(
            f"device and mesh must be of the same type but got {device.type} "
            f"for device and {mesh.device_type} for mesh"
        )
    post_forward_mesh_info = _get_post_forward_mesh_info(
        reshard_after_forward, mesh_info
    )

    state = fully_shard.state(module)
    state.init(module, device)

    managed_modules = _get_managed_modules(module)
    params, buffers = _get_managed_states(managed_modules)
    _move_states_to_device(params, buffers, device, mesh_info)
    if params:
        state._fsdp_param_group = FSDPParamGroup(
            params, module, mesh_info, post_forward_mesh_info, device
        )

    # Place FSDP leftmost for highest priority in the method resolution order
    cls = module.__class__
    dct = {"__deepcopy__": unimplemented_deepcopy}
    new_cls = type(f"FSDP{cls.__name__}", (FSDP, cls), dct)
    module.__class__ = new_cls
    return module


def unimplemented_deepcopy(*args: Any, **kwargs: Any) -> typing_extensions.Never:
    raise AssertionError(
        "FSDP does not support deepcopy. Please use state dict for serialization."
    )


class FSDP:
    def __new__(cls, *args, **kwargs):
        """
        Override ``__new__`` to remove the FSDP class and directly construct
        the original class for cases like indexing into a container module.
        """
        # Use index 2 since 0 is the dynamically constructed `FSDP<...>` class
        # and index 1 is the `FSDP` class itself
        orig_cls = cls.__mro__[2]
        self = orig_cls.__new__(orig_cls, *args, **kwargs)
        self.__init__(*args, **kwargs)
        return self

    def reshard(self) -> None:
        """
        Reshards the module's parameters by freeing unsharded parameters if
        needed. This method is *not* recursive.
        """
        if (state := _get_module_fsdp_state(cast(nn.Module, self))) is None or (
            (fsdp_param_group := state._fsdp_param_group) is None
        ):
            return  # no-op
        fsdp_param_group.reshard()
