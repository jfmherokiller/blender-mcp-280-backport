# Blender MCP - Blender 2.80 backport (minimal core)
#
# Backported from Siddharth Ahuja's blender-mcp addon (www.github.com/ahujasid, (c) 2025)
# to run under Blender 2.80 / Python 3.7.
#
# This is a REDUCED port. It keeps the socket server and the core command
# handlers (get_scene_info, get_object_info, get_viewport_screenshot,
# execute_code) plus a simple Connect/Disconnect sidebar panel, so an MCP
# client on localhost:9876 can drive Blender 2.80. The optional asset
# integrations from the upstream addon (PolyHaven, Hyper3D/Rodin, Sketchfab,
# Hunyuan3D) are intentionally removed: they rely on Blender 3.x+ APIs and
# Python 3.10 `match` syntax that do not exist on 2.80. Their status commands
# are stubbed to "disabled" so the MCP client's startup probes still succeed.

import bpy
import mathutils
import json
import threading
import socket
import time
import traceback
import os
import io
import queue
from bpy.props import IntProperty, BoolProperty
from contextlib import redirect_stdout

bl_info = {
    "name": "Blender MCP (2.80 backport)",
    "author": "BlenderMCP (backport)",
    "version": (1, 2),
    "blender": (2, 80, 0),
    "location": "View3D > Sidebar > BlenderMCP",
    "description": "Connect Blender 2.80 to an MCP client (minimal core backport)",
    "category": "Interface",
}


class BlenderMCPServer:
    def __init__(self, host='localhost', port=9876):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None
        # Commands received on worker threads are handed to the main thread
        # through this queue and drained by the modal operator's TIMER handler
        # (process_pending). All bpy access stays on the main thread. A modal
        # operator timer is used instead of bpy.app.timers because on Blender
        # 2.80 app-timers do not fire while the window is unfocused/idle, while
        # window (GHOST) timers keep posting TIMER events regardless of focus.
        self.command_queue = queue.Queue()
        self.modal_active = False

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
            self.socket.listen(1)

            self.server_thread = threading.Thread(target=self._server_loop)
            self.server_thread.daemon = True
            self.server_thread.start()

            # NB: the main-thread command drain is driven by the modal operator
            # (BLENDERMCP_OT_StartServer), which owns a window TIMER. See
            # process_pending().
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

    def _server_loop(self):
        """Main server loop in a separate thread"""
        print("Server thread started")
        self.socket.settimeout(1.0)  # Timeout to allow for stopping

        while self.running:
            try:
                try:
                    client, address = self.socket.accept()
                    print("Connected to client: {}".format(address))

                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except socket.timeout:
                    continue
                except Exception as e:
                    print("Error accepting connection: {}".format(str(e)))
                    time.sleep(0.5)
            except Exception as e:
                print("Error in server loop: {}".format(str(e)))
                if not self.running:
                    break
                time.sleep(0.5)

        print("Server thread stopped")

    def _handle_client(self, client):
        """Handle connected client"""
        print("Client handler started")
        client.settimeout(None)  # No timeout
        buffer = b''

        try:
            while self.running:
                try:
                    data = client.recv(8192)
                    if not data:
                        print("Client disconnected")
                        break

                    buffer += data
                    try:
                        command = json.loads(buffer.decode('utf-8'))
                        buffer = b''

                        # Hand the command to the main thread for execution.
                        # The reply is sent from _process_queue (main thread).
                        self.command_queue.put((command, client))
                    except json.JSONDecodeError:
                        # Incomplete data, wait for more
                        pass
                except Exception as e:
                    print("Error receiving data: {}".format(str(e)))
                    break
        except Exception as e:
            print("Error in client handler: {}".format(str(e)))
        finally:
            try:
                client.close()
            except Exception:
                pass
            print("Client handler stopped")

    def process_pending(self):
        """
        Drain and execute all queued commands on Blender's MAIN thread.

        Called from the modal operator's TIMER handler. All bpy access happens
        here, on the main thread.
        """
        while True:
            try:
                command, client = self.command_queue.get_nowait()
            except queue.Empty:
                break
            try:
                response = self.execute_command(command)
                client.sendall(json.dumps(response).encode('utf-8'))
            except Exception as e:
                print("Error executing command: {}".format(str(e)))
                traceback.print_exc()
                try:
                    client.sendall(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))
                except Exception:
                    pass

    def execute_command(self, command):
        """Execute a command in the main Blender thread"""
        try:
            return self._execute_command_internal(command)
        except Exception as e:
            print("Error executing command: {}".format(str(e)))
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def _execute_command_internal(self, command):
        """Internal command execution"""
        cmd_type = command.get("type")
        params = command.get("params", {})

        # Only the always-available core handlers are supported in this backport.
        # The asset-integration status probes are stubbed to "disabled" so the
        # MCP client's startup handshake still succeeds.
        handlers = {
            "get_scene_info": self.get_scene_info,
            "get_object_info": self.get_object_info,
            "get_viewport_screenshot": self.get_viewport_screenshot,
            "execute_code": self.execute_code,
            "get_telemetry_consent": self.get_telemetry_consent,
            "get_polyhaven_status": self.get_polyhaven_status,
            "get_hyper3d_status": self.get_hyper3d_status,
            "get_sketchfab_status": self.get_sketchfab_status,
            "get_hunyuan3d_status": self.get_hunyuan3d_status,
        }

        handler = handlers.get(cmd_type)
        if handler:
            try:
                print("Executing handler for {}".format(cmd_type))
                result = handler(**params)
                print("Handler execution complete")
                return {"status": "success", "result": result}
            except Exception as e:
                print("Error in handler: {}".format(str(e)))
                traceback.print_exc()
                return {"status": "error", "message": str(e)}
        else:
            return {"status": "error",
                    "message": "Unknown command type: {} (not supported in the 2.80 backport)".format(cmd_type)}

    # ------------------------------------------------------------------ #
    #  Core handlers                                                     #
    # ------------------------------------------------------------------ #

    def get_scene_info(self):
        """Get information about the current Blender scene"""
        try:
            print("Getting scene info...")
            scene_info = {
                "name": bpy.context.scene.name,
                "object_count": len(bpy.context.scene.objects),
                "objects": [],
                "materials_count": len(bpy.data.materials),
            }

            for i, obj in enumerate(bpy.context.scene.objects):
                if i >= 10:
                    break
                obj_info = {
                    "name": obj.name,
                    "type": obj.type,
                    "location": [round(float(obj.location.x), 2),
                                 round(float(obj.location.y), 2),
                                 round(float(obj.location.z), 2)],
                }
                scene_info["objects"].append(obj_info)

            print("Scene info collected: {} objects".format(len(scene_info['objects'])))
            return scene_info
        except Exception as e:
            print("Error in get_scene_info: {}".format(str(e)))
            traceback.print_exc()
            return {"error": str(e)}

    @staticmethod
    def _get_aabb(obj):
        """Returns the world-space axis-aligned bounding box (AABB) of an object."""
        if obj.type != 'MESH':
            raise TypeError("Object must be a mesh")

        local_bbox_corners = [mathutils.Vector(corner) for corner in obj.bound_box]
        # matrix @ vector works on Blender 2.80's mathutils
        world_bbox_corners = [obj.matrix_world @ corner for corner in local_bbox_corners]

        min_corner = mathutils.Vector(map(min, zip(*world_bbox_corners)))
        max_corner = mathutils.Vector(map(max, zip(*world_bbox_corners)))

        return [[c for c in min_corner], [c for c in max_corner]]

    def get_object_info(self, name):
        """Get detailed information about a specific object"""
        obj = bpy.data.objects.get(name)
        if not obj:
            raise ValueError("Object not found: {}".format(name))

        obj_info = {
            "name": obj.name,
            "type": obj.type,
            "location": [obj.location.x, obj.location.y, obj.location.z],
            "rotation": [obj.rotation_euler.x, obj.rotation_euler.y, obj.rotation_euler.z],
            "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            "visible": obj.visible_get(),
            "materials": [],
        }

        if obj.type == "MESH":
            obj_info["world_bounding_box"] = self._get_aabb(obj)

        for slot in obj.material_slots:
            if slot.material:
                obj_info["materials"].append(slot.material.name)

        if obj.type == 'MESH' and obj.data:
            mesh = obj.data
            obj_info["mesh"] = {
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
            }

        return obj_info

    def get_viewport_screenshot(self, max_size=800, filepath=None, format="png"):
        """
        Capture a screenshot of the current 3D viewport (Blender 2.80 path).

        2.80 has no `bpy.context.temp_override` and no `screen.screenshot_area`;
        instead we pass an explicit context-override dict to `screen.screenshot`
        with `full=False` to capture only the active (VIEW_3D) area.
        """
        try:
            if not filepath:
                return {"error": "No filepath provided"}

            window = bpy.context.window
            screen = bpy.context.screen

            area = None
            for a in screen.areas:
                if a.type == 'VIEW_3D':
                    area = a
                    break
            if not area:
                return {"error": "No 3D viewport found"}

            region = None
            for r in area.regions:
                if r.type == 'WINDOW':
                    region = r
                    break

            override = {"window": window, "screen": screen, "area": area}
            if region is not None:
                override["region"] = region

            # full=False -> capture only the active area (the overridden VIEW_3D)
            bpy.ops.screen.screenshot(override, filepath=filepath, full=False)

            img = bpy.data.images.load(filepath)
            width, height = img.size

            if max(width, height) > max_size:
                scale = max_size / float(max(width, height))
                new_width = int(width * scale)
                new_height = int(height * scale)
                img.scale(new_width, new_height)
                img.file_format = format.upper()
                img.save()
                width, height = new_width, new_height

            bpy.data.images.remove(img)

            return {
                "success": True,
                "width": width,
                "height": height,
                "filepath": filepath,
            }
        except Exception as e:
            return {"error": str(e)}

    def execute_code(self, code):
        """Execute arbitrary Blender Python code"""
        try:
            namespace = {"bpy": bpy}
            capture_buffer = io.StringIO()
            with redirect_stdout(capture_buffer):
                exec(code, namespace)
            captured_output = capture_buffer.getvalue()
            return {"executed": True, "result": captured_output}
        except Exception as e:
            raise Exception("Code execution error: {}".format(str(e)))

    # ------------------------------------------------------------------ #
    #  Stubbed status probes (features not ported)                       #
    # ------------------------------------------------------------------ #

    def get_telemetry_consent(self):
        # No telemetry in the backport.
        return {"consent": False}

    def get_polyhaven_status(self):
        return {"enabled": False, "message": "PolyHaven integration is not available in the Blender 2.80 backport."}

    def get_hyper3d_status(self):
        return {"enabled": False, "message": "Hyper3D integration is not available in the Blender 2.80 backport."}

    def get_sketchfab_status(self):
        return {"enabled": False, "message": "Sketchfab integration is not available in the Blender 2.80 backport."}

    def get_hunyuan3d_status(self):
        return {"enabled": False, "message": "Hunyuan3D integration is not available in the Blender 2.80 backport."}


# ---------------------------------------------------------------------- #
#  UI                                                                     #
# ---------------------------------------------------------------------- #

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
    bl_description = "Start the BlenderMCP server to connect with an MCP client"

    _timer = None
    _server = None

    def execute(self, context):
        # Allow calling from scripts / search menu (no event needed).
        return self.invoke(context, None)

    def invoke(self, context, event):
        global blendermcp_server
        scene = context.scene

        # NOTE: Blender 2.80's `bpy.types` (an RNA wrapper) rejects arbitrary
        # attribute assignment, so we keep the running server in a module-level
        # global rather than on bpy.types (upstream stores it on bpy.types).
        if blendermcp_server is None:
            blendermcp_server = BlenderMCPServer(port=scene.blendermcp_port)

        # Guard against starting a second modal handler (would double-drain).
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
        # Bound to the specific server instance this modal started with, so a
        # disconnect+reconnect can't leave two modals draining one queue.
        srv = self._server
        if srv is None or not srv.running or srv is not blendermcp_server:
            self.cancel(context)
            return {'CANCELLED'}

        if event.type == 'TIMER':
            srv.process_pending()

        # Pass everything through so normal Blender interaction is unaffected.
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


# Module-level handle to the running server instance (see StartServer note).
blendermcp_server = None


classes = (
    BLENDERMCP_PT_Panel,
    BLENDERMCP_OT_StartServer,
    BLENDERMCP_OT_StopServer,
)


def register():
    bpy.types.Scene.blendermcp_port = IntProperty(
        name="Port",
        description="Port for the BlenderMCP server",
        default=9876,
        min=1024,
        max=65535
    )
    bpy.types.Scene.blendermcp_server_running = BoolProperty(
        name="Server Running",
        default=False
    )

    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    global blendermcp_server
    # Make sure the server is stopped before unregistering
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
