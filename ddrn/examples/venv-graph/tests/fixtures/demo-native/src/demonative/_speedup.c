/* An extension module, small enough to read in one sitting.
 *
 * **This file is why the fixture exists.** A node of the graph gets
 * `coreutils`, `unzip`, `tar`, `gzip` and the interpreter, and no compiler.
 * A build of this project therefore fails until the caller names a compiler
 * at the `buildTools` argument of `../../../../default.nix`.
 *
 * The module gives one number. The number proves that the compiler ran, and
 * nothing more is needed to prove that.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

static PyObject *speedup_answer(PyObject *self, PyObject *args)
{
    (void)self;
    (void)args;
    return PyLong_FromLong(42);
}

static PyMethodDef speedup_methods[] = {
    {"answer", speedup_answer, METH_NOARGS, "The number that the compiler built in."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef speedup_module = {
    PyModuleDef_HEAD_INIT,
    "demonative._speedup",
    "One function, compiled from C.",
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
