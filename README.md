# Blender MCP - Blender 2.80 backport (minimal core)

A reduced backport of Siddharth Ahuja's [blender-mcp](https://github.com/ahujasid/blender-mcp)
addon so it runs under **Blender 2.80 / Python 3.7**.

Upstream targets Blender 3.0+ and uses Python 3.10 `match` syntax, neither of
which exists on 2.80. This port keeps the socket server and the core command
handlers and drops the asset integrations that need newer Blender APIs.

## What is kept
- Socket server on `localhost:9876` (same JSON protocol as upstream).
- `execute_code`, `get_scene_info`, `get_object_info`, `get_viewport_screenshot`.
- Connect/Disconnect sidebar panel (View3D > N > BlenderMCP).

## What is removed
- PolyHaven, Hyper3D/Rodin, Sketchfab, Hunyuan3D integrations.
  Their status-probe commands are stubbed to `{"enabled": false}` so an MCP
  client's startup handshake still succeeds.

## Install
Symlinked into the Blender 2.80 addons folder, then enabled via
Preferences > Add-ons ("Blender MCP (2.80 backport)").
