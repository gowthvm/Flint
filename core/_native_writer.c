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
    PyObject *path_obj = NULL;
    PyObject *device_path_obj = NULL;
    wchar_t *path = NULL;
    wchar_t *device_path = NULL;
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

    /* Non-ASCII paths: image and device paths are user data and may contain
     * characters outside the ANSI code page; the "s" format plus CreateFileA
     * mis-resolves those, so both are converted to UTF-16 for the W APIs. */
    if (!PyArg_ParseTupleAndKeywords(
            args, kwargs, "UU|KO", kwlist,
            &path_obj, &device_path_obj, &chunk_size, &progress))
        return NULL;

    path = PyUnicode_AsWideCharString(path_obj, NULL);
    if (path == NULL)
        return NULL;
    device_path = PyUnicode_AsWideCharString(device_path_obj, NULL);
    if (device_path == NULL) {
        PyMem_Free(path);
        return NULL;
    }

    /* FILE_FLAG_NO_BUFFERING requires sector-multiple sizes: clamp and align. */
    if (chunk_size > WRITER_MAX_CHUNK)
        chunk_size = WRITER_MAX_CHUNK;
    if (chunk_size < WRITER_SECTOR)
        chunk_size = WRITER_SECTOR;
    chunk_size -= chunk_size % WRITER_SECTOR;

    in_handle = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL,
                            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (in_handle == INVALID_HANDLE_VALUE)
        goto fail;

    /* Raw device handles are sector aligned; regular files are created (or
     * reopened and truncated) and trimmed back after the padded final chunk. */
    is_device = (_wcsnicmp(device_path, L"\\\\.\\", 4) == 0);
    out_handle = CreateFileW(device_path, GENERIC_WRITE, 0, NULL,
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
        DWORD want = (DWORD)chunk_size;
        DWORD bytes_read = 0;
        DWORD to_write;
        DWORD written_total = 0;
        DWORD io_err = 0;

        /* Stop exactly at the size we sized the source at. */
        if (total > 0 && done >= total)
            break;
        if (total > 0 && (unsigned long long)want > total - done)
            want = (DWORD)(total - done);

        /* The file I/O runs with the GIL released: a single WriteFile with
         * FILE_FLAG_NO_BUFFERING | FILE_FLAG_WRITE_THROUGH can take
         * milliseconds, and pinning the interpreter for every chunk (plus the
         * final FlushFileBuffers) stalls every Python slot in the GUI - the
         * progress signals, the cancel flag, the repaints.  GetLastError()
         * must be sampled while still inside the protected region, because
         * re-acquiring the GIL clobbers it; no goto may cross the macro. */
        /* ReadFile may transfer fewer bytes than asked without being at the
         * end of the file - the very hazard this function already guards on
         * the write side - and a short *mid-file* read that was padded to the
         * next sector would shift every following byte out of place.  Fill
         * the buffer instead; only a read that returns 0 means end of file. */
        Py_BEGIN_ALLOW_THREADS
        while (bytes_read < want) {
            DWORD got = 0;
            if (!ReadFile(in_handle, (char *)buffer + bytes_read,
                          want - bytes_read, &got, NULL)) {
                io_err = GetLastError();
                break;
            }
            if (got == 0)
                break; /* true end of file */
            bytes_read += got;
        }
        Py_END_ALLOW_THREADS
        if (io_err != 0) {
            saved_err = io_err;
            ok = 0;
            goto cleanup;
        }
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
        /* WriteFile can succeed while transferring fewer bytes than asked
         * (raw devices do this at chunk boundaries) and the file pointer
         * still advances by the count written.  `done` below counts the
         * whole chunk, so a short write must be completed here or the
         * target would silently hold less than the reported byte count. */
        Py_BEGIN_ALLOW_THREADS
        while (written_total < to_write) {
            DWORD written = 0;
            if (!WriteFile(out_handle, (char *)buffer + written_total,
                           to_write - written_total, &written, NULL)) {
                io_err = GetLastError();
                break;
            }
            if (written == 0) {
                io_err = ERROR_WRITE_FAULT;
                break;
            }
            written_total += written;
        }
        Py_END_ALLOW_THREADS
        if (io_err != 0) {
            saved_err = io_err;
            ok = 0;
            goto cleanup;
        }
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
        if (bytes_read < want)
            break; /* end of file before `want` bytes - caught below */
    }

    /* A short stream is a failed write, not a successful one.  Without this
     * guard a truncated image was handed back as a normal byte count and the
     * caller reported 100% complete. */
    if (total > 0 && done < total) {
        saved_err = ERROR_HANDLE_EOF;
        ok = 0;
        goto cleanup;
    }

    {
        DWORD flush_err = 0;
        Py_BEGIN_ALLOW_THREADS
        if (!FlushFileBuffers(out_handle))
            flush_err = GetLastError();
        Py_END_ALLOW_THREADS
        if (flush_err != 0) {
            saved_err = flush_err;
            ok = 0;
            goto cleanup;
        }
    }

    if (!is_device) {
        /* The padded final chunk may have extended a regular file past the
         * source size. FILE_FLAG_NO_BUFFERING forbids misaligned seeks, so
         * trim the file through a fresh buffered handle. */
        HANDLE trim;
        CloseHandle(out_handle);
        out_handle = INVALID_HANDLE_VALUE;
        trim = CreateFileW(device_path, GENERIC_WRITE, 0, NULL,
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
    PyMem_Free(path);
    PyMem_Free(device_path);
    if (ok == -1)
        return NULL; /* exception already set by the callback */
    if (!ok) {
        if (saved_err != 0)
            PyErr_SetFromWindowsErr(saved_err);
        else
            PyErr_SetString(PyExc_OSError, "native write failed");
        return NULL;
    }
    return PyLong_FromUnsignedLongLong(done);

fail:
    /* VirtualFree/CloseHandle in the cleanup block overwrite the thread's
     * last-error value, so it must be captured here, before the goto. */
    saved_err = GetLastError();
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
