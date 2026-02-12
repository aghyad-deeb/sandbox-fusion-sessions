# conftest.py — stub out flash_attn before transformers tries to load it.
# This is needed on machines where the flash_attn CUDA extension was compiled
# against a different PyTorch ABI.

import types
import sys


def _stub_flash_attn():
    """Install a minimal flash_attn fake so transformers can import without CUDA."""
    if "flash_attn" in sys.modules:
        return  # already loaded (real or fake)

    def _noop(*a, **kw):
        raise RuntimeError("flash_attn stub: not a real implementation")

    fa = types.ModuleType("flash_attn")
    fa.__spec__ = types.SimpleNamespace(
        name="flash_attn", loader=None, origin=None, submodule_search_locations=[]
    )
    fa.__path__ = []
    fa.__version__ = "2.0.0"
    fa.flash_attn_func = _noop
    fa.flash_attn_varlen_func = _noop
    sys.modules["flash_attn"] = fa

    bp = types.ModuleType("flash_attn.bert_padding")
    bp.__spec__ = types.SimpleNamespace(name="flash_attn.bert_padding", loader=None, origin=None)
    bp.index_first_axis = _noop
    bp.pad_input = _noop
    bp.unpad_input = _noop
    sys.modules["flash_attn.bert_padding"] = bp
    fa.bert_padding = bp

    fi = types.ModuleType("flash_attn.flash_attn_interface")
    fi.__spec__ = types.SimpleNamespace(name="flash_attn.flash_attn_interface", loader=None, origin=None)
    sys.modules["flash_attn.flash_attn_interface"] = fi
    fa.flash_attn_interface = fi

    cuda = types.ModuleType("flash_attn_2_cuda")
    cuda.__spec__ = types.SimpleNamespace(name="flash_attn_2_cuda", loader=None, origin=None)
    sys.modules["flash_attn_2_cuda"] = cuda


_stub_flash_attn()
