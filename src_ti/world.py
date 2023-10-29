import torch
import taichi as ti
import numpy as np
import warnings

class Channel:
    def __init__(
            self, id=None, ti_dtype=ti.f32,
            init_func=None,
            lims=None,
            metadata: dict=None, **kwargs):
        self.id = id
        self.lims = lims if lims else (-np.inf, np.inf)
        self.metadata = metadata if metadata is not None else {}
        self.metadata.update(kwargs)
        self.init_func = init_func
        self.ti_dtype = ti_dtype
        self.memblock = None
        self.indices = None
    
    def index(self, indices, memblock):
        self.memblock = memblock
        if len(indices) == 1:
            indices = indices[0]
        self.indices = indices
        self.metadata['indices'] = indices
    
    def add_subchannel(self, id, ti_dtype=ti.f32, **kwargs):
        subch = Channel(id=id, ti_dtype=ti_dtype, **kwargs)
        subch.metadata['parent'] = self
        self.metadata[id] = subch
        self.metadata['subchids'] = self.metadata.get('subchids', [])
        self.metadata['subchids'].append(id)
        return subch

    def get_data(self):
        if self.memblock is None:
            raise ValueError(f"Channel: Channel {self.id} has not been allocated yet.")
        else:
            return self.memblock[self.indices]

    def __getitem__(self, key):
        return self.metadata.get(key)

    def __setitem__(self, key, value):
        self.metadata[key] = value
            
@ti.data_oriented
class World:
    # TODO: Support multi-level indexing beyond 2 levels
    # TODO: Support mixed taichi and torch tensors - which will be transferred more?
    def __init__(self, shape, torch_dtype, torch_device, channels: dict=None):
        self.shape = (*shape, 0)
        self.torch_dtype = torch_dtype
        self.torch_device = torch_device
        self.channels = {}
        self.memory_allocated = False
        if channels is not None:
            self.add_channels(channels)
        self.tensor_dict = None
        self.mem = None
        self.data = None
        self.index = None

    def add_channel(self, id: str, ti_dtype=ti.f32, **kwargs):
        if self.memory_allocated:
            raise ValueError(f"World: When adding channel {id}: Cannot add channel after world memory is allocated (yet).")
        self.channels[id] = Channel(id=id, ti_dtype=ti_dtype, **kwargs)

    def add_channels(self, channels: dict):
        if self.memory_allocated:
            raise ValueError(f"World: When adding channels {channels}: Cannot add channels after world memory is allocated (yet).")
        for chid in channels.keys():
            ch = channels[chid]
            if isinstance(ch, Channel):
                 if ch.id is None:
                     ch.id = chid
                 self.channels[id] = ch
            elif isinstance(ch, dict):
                self.add_channel(chid, **ch)
            else:
                self.add_channel(chid, ch)
        
    def check_ch_shape(self, shape):
        lshape = len(shape)
        if lshape > 3 or lshape < 2:
            raise ValueError(f"World: Channel shape must be 2 or 3 dimensional. Got shape: {shape}")
        if shape[:2] != self.shape[:2]:
            print(shape[:2], self.shape[:2])
            raise ValueError(f"World: Channel shape must be (w, h, ...) where w and h are the world dimensions: {self.shape}. Got shape: {shape}")
        if lshape == 2:
            return 1
        else:
            return shape[2]

    def _transfer_to_mem(self, mem, tensor_dict, index_tree, channel_dict):
        for chid, chindices in index_tree.items():
            if 'subchannels' in chindices:
                for subchid, subchtree in chindices['subchannels'].items():
                    if tensor_dict[chid][subchid].dtype != self.torch_dtype:
                        warnings.warn(f"World: Warning: The Torch dtype of channel {chid} ({tensor_dict[chid].dtype}) does not match the Torch dtype of its world ({self.torch_dtype}). Casting to {self.torch_dtype}.")
                    if len(tensor_dict[chid][subchid].shape) == 2:
                        tensor_dict[chid][subchid] = tensor_dict[chid][subchid].unsqueeze(2)
                    mem[:, :, subchtree['indices']] = tensor_dict[chid][subchid].type(self.torch_dtype)
                    channel_dict[chid].add_subchannel(subchid, ti_dtype=channel_dict[chid].ti_dtype)
                    channel_dict[chid][subchid].index(subchtree['indices'], mem)
                channel_dict[chid].index(chindices['indices'], mem)
            else:
                if tensor_dict[chid].dtype != self.torch_dtype:
                    warnings.warn(f"World: Warning: The Torch dtype of channel {chid} ({tensor_dict[chid].dtype}) does not match the Torch dtype of its world ({self.torch_dtype}). Casting to {self.torch_dtype}.")
                if len(tensor_dict[chid].shape) == 2:
                    tensor_dict[chid] = tensor_dict[chid].unsqueeze(2)
                mem[:, :, chindices['indices']] = tensor_dict[chid].type(self.torch_dtype)
                channel_dict[chid].index(chindices['indices'], mem)
        return mem, channel_dict
    
    def _index_subchannels(self, subchdict, start_ind, parent_chid):
        end_ind = start_ind
        subch_tree = {}
        for subchid, subch in subchdict.items():
            if not isinstance(subch, torch.Tensor):
                raise ValueError(f"World: Channel grouping only supported up to a depth of 2. Subchannel {subchid} of channel {parent_chid} must be a torch.Tensor. Got type: {type(subch)}")
            subch_depth = self.check_ch_shape(subch.shape)
            subch_tree[subchid] = {
                'indices': [i for i in range(end_ind, end_ind+subch_depth)]
            }
            end_ind += subch_depth
        return subch_tree, end_ind-start_ind

    def malloc(self):
        if self.memory_allocated:
            raise ValueError(f"World: Cannot allocate world memory twice.")
        celltype = ti.types.struct(**{chid: self.channels[chid].ti_dtype for chid in self.channels.keys()})
        tensor_dict = celltype.field(shape=self.shape[:2]).to_torch(device=self.torch_device)

        index_tree = {}
        endlayer_pointer = self.shape[2]
        for chid, chdata in tensor_dict.items():
            if isinstance(chdata, torch.Tensor):
                ch_depth = self.check_ch_shape(chdata.shape)
                index_tree[chid] = {'indices': [i for i in range(endlayer_pointer, endlayer_pointer + ch_depth)]}
                endlayer_pointer += ch_depth
            elif isinstance(chdata, dict):
                subch_tree, total_depth = self._index_subchannels(chdata, endlayer_pointer, chid)
                index_tree[chid] = {
                    'subchannels': subch_tree,
                    'indices': [i for i in range(endlayer_pointer, endlayer_pointer + total_depth)]
                }
                endlayer_pointer += total_depth
                
        mem = torch.empty((*self.shape[:2], endlayer_pointer), dtype=self.torch_dtype, device=self.torch_device)
        self.mem, self.channels = self._transfer_to_mem(mem, tensor_dict, index_tree, self.channels)
        # self.mem = self.mem.permute(2, 0, 1)
        # self.shape = self.mem.shape
        del tensor_dict
        self.index = self._windex(index_tree)
        self.data = self._wdata(self.mem, self.index)
        return self.mem, self.data, self.index
    
    def __getitem__(self, key):
        return self.channels.get(key)

    class _windex:
        def __init__(self, index_tree):
            self.index_tree = index_tree

        def _get_tuple_inds(self, key_tuple):
            chid = key_tuple[0]
            subchid = key_tuple[1]
            if isinstance(subchid, list):
                inds = []
                for subchid_single in key_tuple[1]:
                    inds += self.index_tree[chid]['subchannels'][subchid_single]['indices']
            else:
                inds = self.index_tree[chid]['subchannels'][subchid]['indices']
            return inds

        def __getitem__(self, key):
            if isinstance(key, tuple):
                return self._get_tuple_inds(key)
            elif isinstance(key, list):
                inds = []
                for chid in key:
                    if isinstance(chid, tuple):
                        inds += self._get_tuple_inds(chid)
                    else:
                        inds += self.index_tree[chid]['indices']
                return inds
            else:
                return self.index_tree[key]['indices']
        
        def __setitem__(self, key, value):
            raise ValueError(f"World: Cannot set world data/indices directly. Use world.add_channels() or world.add_channel() to add channels to the world.")
    
    class _wdata:
        def __init__(self, mem, ind):
            self.mem = mem
            self.ind = ind
        
        def __getitem__(self, key):
            return self.mem[:,:,self.ind[key]]
        
        def __setitem__(self, key, value):
            raise ValueError("World: Cannot set world data/indices directly. Use world.add_channels() or world.add_channel() to add channels to the world.")
    

    def __setitem__(self, key, value):
        if self.mem is not None:
            raise ValueError("World: Cannot add channels after world memory is allocated (yet).")
        else:
            self.add_channels({key: value})