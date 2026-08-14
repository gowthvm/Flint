/*
 * core/_native_writer.c - optional native writer for Flint.
 *
 * Exposes native_write(path, device_path, chunk_size, progress=None) which
 * copies `path` onto `device_path` using CreateFile/ReadFile/WriteFile with
 * FILE_FLAG_NO_BUFFERING and a sector-aligned buffer for the highest raw
 * write throughput on Windows.
 *
 * The module is optional: when it is not built, core/writer.py falls back to
 * pure-Python buffered writes automatically.
 *
 * Build (from the repository root):
 *   python setup.py build_ext --inplace
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <windows.h>
#include <string.h>

/* Alignment granularity used with FILE_FLAG_NO_BUFFERING (typical sector). */
#define WRITER_SECTOR 4096
#define WRITER_MAX_CHUNK (256ULL * 1024 * 1024)
#define WRITER_DEFAULT_CHUNK (8ULL * 1024 * 1024)

static PyObject *
py_native_write(PyObject *self, PyObject *args, PyObject *kwargs)
{
    const char *path;
    const char *device_path;
    unsigned long long chunk_size = WRITER_DEFAULT_CHUNK;
    PyObject *progress = Py_None;
    static char *kwlist[] = {"path", "device_path", "chunk_size", "progress", NULL};
    HANDLE in_handle = INVALID_HANDLE_VALUE;
    HANDLE out_handle = INVALID_HANDLE_VALUE;
    LPVOID buffer = NULL;
    unsigned long long total = 0;
    unsigned long long done = 0;
    int is_device = 0;
    int ok = 0;
    DWORD saved_err = 0;

    (void)self;

    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "ss|KO", kwlist,
            &path, &device_path, &chunk_size, &progress))
        return NULL;

    /* FILE_FLAG_NO_BUFFERING requires sector-multiple sizes: clamp and align. */
    if (chunk_size > WRITER_MAX_CHUNK)
        chunk_size = WRITER_MAX_CHUNK;
    if (chunk_size < WRITER_SECTOR)
        chunk_size = WRITER_SECTOR;
    chunk_size -= chunk_size % WRITER_SECTOR;

    in_handle = CreateFileA(path, GENERIC_READ, FILE_SHARE_READ, NULL,
                            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (in_handle == INVALID_HANDLE_VALUE)
        goto fail;

    /* Raw device handles are sector aligned; regular files are created (or
     * reopened and truncated) and trimmed back after the padded final chunk. */
    is_device = (_strnicmp(device_path, "\\\\.\\", 4) == 0);
    out_handle = CreateFileA(device_path, GENERIC_WRITE, 0, NULL,
                             is_device ? OPEN_EXISTING : OPEN_ALWAYS,
                             FILE_FLAG_NO_BUFFERING | FILE_FLAG_WRITE_THROUGH,
                             NULL);
    if (out_handle == INVALID_HANDLE_VALUE)
        goto fail;

    if (!is_device) {
        LARGE_INTEGER zero;
        zero.QuadPart = 0;
        if (!SetFilePointerEx(out_handle, zero, NULL, FILE_BEGIN) ||
            !SetEndOfFile(out_handle))
            goto fail;
    }

    /* VirtualAlloc returns page-aligned memory (>= 4096 byte alignment). */
    buffer = VirtualAlloc(NULL, (SIZE_T)chunk_size,
                          MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (buffer == NULL)
        goto fail;

    {
        LARGE_INTEGER size;
        if (!GetFileSizeEx(in_handle, &size))
            goto fail;
        total = (unsigned long long)size.QuadPart;
    }

    for (;;) {
        DWORD bytes_read = 0;
        DWORD to_write;
        DWORD written = 0;

        if (!ReadFile(in_handle, buffer, (DWORD)chunk_size, &bytes_read, NULL))
            goto fail;
        if (bytes_read == 0)
            break;
        to_write = bytes_read;
        if (to_write % WRITER_SECTOR != 0) {
            /* The final partial chunk must still be written sector-aligned;
             * pad the tail with zeros (like dd) instead of stale data. */
            to_write += WRITER_SECTOR - to_write % WRITER_SECTOR;
            memset((char *)buffer + bytes_read, 0,
                   to_write - bytes_read);
        }
        if (!WriteFile(out_handle, buffer, to_write, &written, NULL))
            goto fail;
        done += bytes_read;

        if (progress != NULL && progress != Py_None) {
            PyObject *result =
                PyObject_CallFunction(progress, "KK", done, total);
            if (result == NULL) {
                ok = -1; /* propagate the Python exception */
                goto cleanup;
            }
            Py_DECREF(result);
        }
        if (bytes_read < (DWORD)chunk_size)
            break;
    }

    if (!FlushFileBuffers(out_handle))
        goto fail;

    if (!is_device) {
        /* The padded final chunk may have extended a regular file past the
         * source size. FILE_FLAG_NO_BUFFERING forbids misaligned seeks, so
         * trim the file through a fresh buffered handle. */
        HANDLE trim;
        CloseHandle(out_handle);
        out_handle = INVALID_HANDLE_VALUE;
        trim = CreateFileA(device_path, GENERIC_WRITE, 0, NULL,
                           OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
        if (trim == INVALID_HANDLE_VALUE)
            goto fail;
        {
            LARGE_INTEGER pos;
            pos.QuadPart = (LONGLONG)done;
            if (!SetFilePointerEx(trim, pos, NULL, FILE_BEGIN) ||
                !SetEndOfFile(trim)) {
                /* go through cleanup so the buffer and in_handle are
                 * released; saved_err survives CloseHandle clobbering it */
                DWORD err = GetLastError();
                CloseHandle(trim);
                trim = INVALID_HANDLE_VALUE;
                saved_err = err;
                ok = 0;
                goto cleanup;
            }
        }
        CloseHandle(trim);
    }

    ok = 1;

cleanup:
    if (buffer != NULL)
        VirtualFree(buffer, 0, MEM_RELEASE);
    if (out_handle != INVALID_HANDLE_VALUE)
        CloseHandle(out_handle);
    if (in_handle != INVALID_HANDLE_VALUE)
        CloseHandle(in_handle);
    if (ok == -1)
        return NULL; /* exception already set by the callback */
    if (!ok) {
        if (saved_err != 0)
            PyErr_SetFromWindowsErr(saved_err);
        else
            PyErr_SetFromWindowsErr(GetLastError());
        return NULL;
    }
    return PyLong_FromUnsignedLongLong(done);

fail:
    ok = 0;
    goto cleanup;
}

static PyMethodDef native_writer_methods[] = {
    {"native_write",
     (PyCFunction)(void (*)(void))py_native_write,
     METH_VARARGS | METH_KEYWORDS,
     "native_write(path, device_path, chunk_size=8388608, progress=None)\n"
     "Copy ``path`` onto ``device_path`` in aligned ``chunk_size`` buffers.\n"
     "The destination is opened with FILE_FLAG_NO_BUFFERING and the buffer\n"
     "is sector aligned; chunk sizes are rounded to multiples of 4096 bytes.\n"
     "Returns the number of bytes written. ``progress`` is an optional\n"
     "callable invoked as ``progress(bytes_done, bytes_total)`` after every\n"
     "chunk; raising from it aborts the write."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef native_writer_module = {
    PyModuleDef_HEAD_INIT,
    "_native_writer",
    "Native aligned writer for Flint (optional extension).",
    -1,
    native_writer_methods,
};

PyMODINIT_FUNC
PyInit__native_writer(void)
{
    return PyModule_Create(&native_writer_module);
}
