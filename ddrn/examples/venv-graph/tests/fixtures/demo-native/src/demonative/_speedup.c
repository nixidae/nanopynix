/* An extension module, small enough to read in one sitting.
 *
 * **This file is why the fixture exists.** A node of the graph gets
 * `coreutils`, `unzip`, `tar`, `gzip` and the interpreter, and no compiler.
 * A build of this project therefore fails until the caller names a compiler
 * at the `buildTools` argument of `../../../../default.nix`.
 *
 * **It also reads a header and a symbol of a library that the node does not
 * hold either.** `zlib.h` and `zlibVersion` come from `CPPFLAGS` and from
 * `LDFLAGS`, which `../../../graph.nix` writes out of the same argument. So a
 * build that found the compiler and lost the library fails here, and
 * `ddrn/PACKAGING-FACTS.md` records the version of this file that did.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <zlib.h>

static PyObject *speedup_answer(PyObject *self, PyObject *args)
{
    (void)self;
    (void)args;
    return PyLong_FromLong(42);
}

static PyObject *speedup_library(PyObject *self, PyObject *args)
{
    (void)self;
    (void)args;
    return PyUnicode_FromString(zlibVersion());
}

static PyMethodDef speedup_methods[] = {
    {"answer", speedup_answer, METH_NOARGS, "The number that the compiler built in."},
    {"library", speedup_library, METH_NOARGS, "The version of the library that the linker found."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef speedup_module = {
    PyModuleDef_HEAD_INIT,
    "demonative._speedup",
    "Two functions, compiled from C.",
    -1,
    speedup_methods,
    NULL,
    NULL,
    NULL,
    NULL,
};

PyMODINIT_FUNC PyInit__speedup(void)
{
    return PyModule_Create(&speedup_module);
}
