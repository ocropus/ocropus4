import importlib
import torch
import io
import tempfile


def torch_dumps(obj):
    """Dump a data structure to a string using torch.save."""
    buf = io.BytesIO()
    torch.save(obj, buf)
    return buf.getbuffer()


def torch_loads(buf):
    """Load a data structure from a string using torch.load."""
    return torch.load(io.BytesIO(buf))


def read_file(fname):
    """Read a file into memory."""
    with open(fname) as stream:
        return stream.read()


num_modules = 0


def load_module(file_name, module_name=None):
    """Load a module from a file."""
    global num_modules
    if module_name is None:
        module_name = "__mod{num_modules}"
        num_modules += 1
    loader = importlib.machinery.SourceFileLoader(module_name, file_name)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def make_module(text, module_name=None):
    """Create a module from a string containing source code."""
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".py") as stream:
        stream.write(text)
        stream.flush()
        return load_module(stream.name, module_name=module_name)


def make_model(src, *args, fun_name="make_model", **kw):
    """Instantiate a PyTorch model from Python source code."""
    if src.endswith(".py"):
        src = read_file(src)
    mmod = make_module(src)
    model = getattr(mmod, fun_name)(*args, **kw)
    model.msrc_ = src
    model.margs_ = (args, kw)
    return model


def save_model(model, fname):
    """Save a PyTorch model (parameters and source code) to a file."""
    state = dict(msrc=model.msrc_, margs=model.margs_, mstate=model.state_dict())
    torch.save(state, fname)


def dump_model(model):
    """Dump a model to a string using torch.save."""
    buf = io.BytesIO()
    save_model(model, buf)
    return buf.getbuffer()


def load_or_make_model(fname, *args, fun_name="make_model", **kw):
    """Load a model from ".pth" file or instantiate it from a ".py" file."""
    if fname.endswith(".pth"):
        state = torch.load(fname)
        args, kw = state.get("margs", ([], {}))
        model = make_model(state["msrc"], *args, fun_name=fun_name, **kw)
        model.load_state_dict(state["mstate"])
        return model
    else:
        return make_model(fname, *args, **kw)