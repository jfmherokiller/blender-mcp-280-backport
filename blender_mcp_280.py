# Blender MCP - Blender 2.80 backport
#
# A Blender 2.80 / Python 3.7 compatible add-on that speaks the *official*
# Blender MCP socket protocol (projects.blender.org/lab/blender_mcp), so the
# official Blender MCP server (localhost:9876) can drive Blender 2.80.
#
# The official add-on is a Python package that targets a recent Blender and
# uses Python 3.10+ typing (`X | None` in signatures, etc.), which will not run
# under 2.80's Python 3.7. Fortunately its socket server implements exactly ONE
# request type - `{"type": "execute", "code": ..., "strict_json": bool}` - and
# every MCP tool (execute_blender_code, get_blendfile_summary, object summaries,
# screenshots, ...) is just server-side Python sent through that one channel,
# reading back a `result` dict. So this single-file add-on reimplements just
# that protocol, using API available in Blender 2.80.
#
# Protocol (null-byte-delimited JSON both directions):
#   request : {"type": "execute", "code": "<python>", "strict_json": <bool>}\0
#   response: {"status": "ok", "result": <dict>[, "stdout", "stderr"]}\0
#          or {"status": "error", "message": "<traceback/message>"[, ...]}\0
# The executed code assigns a JSON-serialisable dict to a variable `result`.
#
# NOTE: deferred responses (code that defines `check_is_finished`, used by some
# long-running tools like renders) are NOT supported here - they return a clear
# "unsupported" error, matching the official server's background-mode behaviour.

import bpy
import json
import socket
import threading
import time
import traceback
import sys
import io
import queue
from bpy.props import IntProperty, BoolProperty

bl_info = {
    "name": "Blender MCP (2.80 backport)",
    "author": "BlenderMCP (backport)",
    "version": (2, 0),
    "blender": (2, 80, 0),
    "location": "View3D > Sidebar > BlenderMCP",
    "description": "Connect Blender 2.80 to the official Blender MCP server (localhost:9876)",
    "category": "Interface",
}

# Print each request/response to the system console when True.
DEBUG = False

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876
_CLIENT_TIMEOUT = 10.0            # seconds to receive a complete request
_RECV_BUFFER_SIZE = 4096
_MAX_REQUEST_BYTES = 10 * 1024 * 1024  # 10 MiB
_DEFERRED_UNSUPPORTED_MESSAGE = (
    "Deferred responses via `check_is_finished` are not supported by the "
    "Blender 2.80 backport add-on. Finish the request synchronously instead."
)


class _Tee(io.TextIOBase):
    """Write to both a buffer and the original stream (so prints still show in the console)."""

    def __init__(self, original):
        self._buffer = io.StringIO()
        self._original = original

    def write(self, s):
        try:
            self._original.write(s)
        except Exception:
            pass
        return self._buffer.write(s)

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass
        self._buffer.flush()

    def getvalue(self):
        return self._buffer.getvalue()


def _blocked_exit(*args, **kwargs):
    raise RuntimeError("sys.exit() is not allowed in MCP-executed code")


class BlenderMCPServer:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None
        # Worker threads read a full request off the socket and hand
        # (request_bytes, client_socket) to the main thread through this queue.
        # process_pending() (driven by the modal operator's window TIMER) drains
        # it, so all bpy access happens on the main thread. A window/GHOST timer
        # is used rather than bpy.app.timers because the latter does not fire
        # while Blender is unfocused on 2.80.
        self.command_queue = queue.Queue()
        self.modal_active = False

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def start(self):
        if bpy.app.background:
            print("BlenderMCP: cannot start server in background mode (blender -b) - commands would never execute")
            return
        if self.running:
            print("Server is already running")
            return

        self.running = True
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.socket.listen(5)

            self.server_thread = threading.Thread(target=self._server_loop)
            self.server_thread.daemon = True
            self.server_thread.start()

            # The main-thread command drain is driven by the modal operator's
            # window TIMER (see BLENDERMCP_OT_StartServer / process_pending).
            print("BlenderMCP server started on {}:{}".format(self.host, self.port))
        except Exception as e:
            print("Failed to start server: {}".format(str(e)))
            self.stop()

    def stop(self):
        self.running = False
        # The modal operator watches self.running and tears down its own timer.
        if self.socket:
            try:
                self.socket.close()
            except Exception:
                pass
            self.socket = None
        if self.server_thread:
            try:
                if self.server_thread.is_alive():
                    self.server_thread.join(timeout=1.0)
            except Exception:
                pass
            self.server_thread = None
        print("BlenderMCP server stopped")

    # ------------------------------------------------------------------ #
    #  Networking (worker threads)                                        #
    # ------------------------------------------------------------------ #

    def _server_loop(self):
        print("Server thread started")
        self.socket.settimeout(1.0)  # allow periodic checks of self.running
        while self.running:
            try:
                try:
                    client, address = self.socket.accept()
                except socket.timeout:
                    continue
                except Exception as e:
                    if self.running:
                        print("Error accepting connection: {}".format(str(e)))
                    time.sleep(0.5)
                    continue
                if DEBUG:
                    print("Connected to client: {}".format(address))
                t = threading.Thread(target=self._handle_client, args=(client,))
                t.daemon = True
                t.start()
            except Exception as e:
                if not self.running:
                    break
                print("Error in server loop: {}".format(str(e)))
                time.sleep(0.5)
        print("Server thread stopped")

    def _handle_client(self, client):
        """
        Read a single null-terminated request, then hand it to the main thread.

        The official protocol is one request per connection; the response is
        sent (and the socket closed) by process_pending on the main thread.
        """
        client.settimeout(_CLIENT_TIMEOUT)
        buffer = bytearray()
        try:
            while self.running and b"\0" not in buffer:
                chunk = client.recv(_RECV_BUFFER_SIZE)
                if not chunk:
                    # Disconnected before a complete request arrived.
                    self._safe_close(client)
                    return
                buffer.extend(chunk)
                if len(buffer) > _MAX_REQUEST_BYTES:
                    self._send_and_close(
                        client,
                        {"status": "error",
                         "message": "Request exceeds {} byte limit".format(_MAX_REQUEST_BYTES)},
                    )
                    return
            if not self.running:
                self._safe_close(client)
                return
            request_data = bytes(buffer[:buffer.index(b"\0")])
            # Execution (and the reply) happen on the main thread.
            self.command_queue.put((request_data, client))
        except socket.timeout:
            self._send_and_close(client, {"status": "error", "message": "Client timed out"})
        except Exception as e:
            print("Error in client handler: {}".format(str(e)))
            self._safe_close(client)

    @staticmethod
    def _safe_close(client):
        try:
            client.close()
        except Exception:
            pass

    def _send_and_close(self, client, response):
        try:
            client.sendall(json.dumps(response).encode("utf-8") + b"\0")
        except Exception:
            pass
        self._safe_close(client)

    # ------------------------------------------------------------------ #
    #  Execution (main thread, via modal TIMER)                           #
    # ------------------------------------------------------------------ #

    def process_pending(self):
        """Drain the queue and execute each request on Blender's MAIN thread."""
        while True:
            try:
                request_data, client = self.command_queue.get_nowait()
            except queue.Empty:
                break
            try:
                response = self._execute_request(request_data)
            except Exception:
                response = {"status": "error", "message": traceback.format_exc()}
            self._send_and_close(client, response)

    def _execute_request(self, data):
        try:
            request = json.loads(data)
        except Exception:
            return {"status": "error", "message": "Invalid JSON request"}

        if request.get("type") != "execute":
            return {"status": "error",
                    "message": "Unknown request type: {!r}".format(request.get("type"))}

        code = request.get("code", "")
        strict_json = request.get("strict_json")
        if not isinstance(strict_json, bool):
            return {"status": "error",
                    "message": ("Internal error: request is missing the required 'strict_json' "
                                "boolean key.")}

        if DEBUG:
            print("request:\n{}".format(code), file=sys.stderr)
        response = self._execute_code(code, strict_json)
        if DEBUG:
            print("response: {}".format(json.dumps(response)[:500]), file=sys.stderr)
        return response

    def _execute_code(self, code, strict_json):
        namespace = {"result": {}}

        old_out, old_err, old_exit = sys.stdout, sys.stderr, sys.exit
        tee_out, tee_err = _Tee(old_out), _Tee(old_err)
        sys.stdout, sys.stderr = tee_out, tee_err
        sys.exit = _blocked_exit
        exec_error = None
        try:
            try:
                exec(code, namespace)
            except Exception:
                exec_error = traceback.format_exc()
        finally:
            sys.stdout, sys.stderr, sys.exit = old_out, old_err, old_exit

        if exec_error is not None:
            response = {"status": "error", "message": exec_error}
            return self._attach_output(response, tee_out, tee_err)

        # Deferred responses (background jobs) are not supported here.
        check_fn = namespace.get("check_is_finished")
        if callable(check_fn):
            response = {"status": "error", "message": _DEFERRED_UNSUPPORTED_MESSAGE}
            return self._attach_output(response, tee_out, tee_err)

        result = namespace.get("result")
        if not isinstance(result, dict):
            response = {
                "status": "error",
                "message": ("The `result` variable must be a dict, not {}. "
                            "Wrap your return value: result = {{\"key\": value}}"
                            ).format(type(result).__name__),
            }
            return self._attach_output(response, tee_out, tee_err)

        if strict_json:
            try:
                json.dumps(result)
            except (TypeError, ValueError) as ex:
                response = {"status": "error",
                            "message": "The `result` value is not JSON-serializable: {}".format(str(ex))}
            else:
                response = {"status": "ok", "result": result}
        else:
            # repr fallback so non-serializable objects (e.g. Blender IDs) survive.
            result = json.loads(json.dumps(result, default=repr))
            response = {"status": "ok", "result": result}
        return self._attach_output(response, tee_out, tee_err)

    @staticmethod
    def _attach_output(response, tee_out, tee_err):
        so = tee_out.getvalue()
        se = tee_err.getvalue()
        if so:
            response["stdout"] = so
        if se:
            response["stderr"] = se
        return response


# Module-level handle to the running server instance. (Blender 2.80's bpy.types
# is a read-only RNA wrapper and rejects arbitrary attribute assignment, so the
# server cannot be stored there the way the upstream addons do.)
blendermcp_server = None


# ---------------------------------------------------------------------------- #
#  UI                                                                          #
# ---------------------------------------------------------------------------- #

class BLENDERMCP_PT_Panel(bpy.types.Panel):
    bl_label = "Blender MCP"
    bl_idname = "BLENDERMCP_PT_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'BlenderMCP'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.prop(scene, "blendermcp_port")
        if not scene.blendermcp_server_running:
            layout.operator("blendermcp.start_server", text="Connect to MCP server")
        else:
            layout.operator("blendermcp.stop_server", text="Disconnect from MCP server")
            layout.label(text="Running on port {}".format(scene.blendermcp_port))


class BLENDERMCP_OT_StartServer(bpy.types.Operator):
    bl_idname = "blendermcp.start_server"
    bl_label = "Connect to MCP server"
    bl_description = "Start the BlenderMCP server to connect with the Blender MCP client"

    _timer = None
    _server = None

    def execute(self, context):
        return self.invoke(context, None)

    def invoke(self, context, event):
        global blendermcp_server
        scene = context.scene

        if blendermcp_server is None:
            blendermcp_server = BlenderMCPServer(port=scene.blendermcp_port)

        if blendermcp_server.running and blendermcp_server.modal_active:
            self.report({'INFO'}, "BlenderMCP server already running")
            return {'CANCELLED'}

        blendermcp_server.start()
        scene.blendermcp_server_running = blendermcp_server.running
        if not blendermcp_server.running:
            return {'CANCELLED'}

        # Drive the main-thread command drain from a window TIMER. Unlike
        # bpy.app.timers, window timers keep firing when Blender is unfocused
        # on 2.80, so MCP calls are serviced even while you work elsewhere.
        wm = context.window_manager
        self._server = blendermcp_server
        self._timer = wm.event_timer_add(0.05, window=context.window)
        wm.modal_handler_add(self)
        blendermcp_server.modal_active = True
        print("BlenderMCP modal command-processor started")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        srv = self._server
        if srv is None or not srv.running or srv is not blendermcp_server:
            self.cancel(context)
            return {'CANCELLED'}
        if event.type == 'TIMER':
            srv.process_pending()
        return {'PASS_THROUGH'}

    def cancel(self, context):
        wm = context.window_manager
        if self._timer is not None:
            try:
                wm.event_timer_remove(self._timer)
            except Exception:
                pass
            self._timer = None
        if self._server is not None:
            self._server.modal_active = False
        self._server = None
        print("BlenderMCP modal command-processor stopped")


class BLENDERMCP_OT_StopServer(bpy.types.Operator):
    bl_idname = "blendermcp.stop_server"
    bl_label = "Disconnect from MCP server"
    bl_description = "Stop the BlenderMCP server"

    def execute(self, context):
        global blendermcp_server
        scene = context.scene
        if blendermcp_server is not None:
            blendermcp_server.stop()
            blendermcp_server = None
        scene.blendermcp_server_running = False
        return {'FINISHED'}


classes = (
    BLENDERMCP_PT_Panel,
    BLENDERMCP_OT_StartServer,
    BLENDERMCP_OT_StopServer,
)


def register():
    bpy.types.Scene.blendermcp_port = IntProperty(
        name="Port",
        description="Port for the BlenderMCP server",
        default=DEFAULT_PORT,
        min=1024,
        max=65535,
    )
    bpy.types.Scene.blendermcp_server_running = BoolProperty(
        name="Server Running",
        default=False,
    )
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    global blendermcp_server
    if blendermcp_server is not None:
        try:
            blendermcp_server.stop()
        except Exception:
            pass
        blendermcp_server = None
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.blendermcp_port
    del bpy.types.Scene.blendermcp_server_running


if __name__ == "__main__":
    register()
