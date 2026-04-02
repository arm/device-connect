"""Device Connect Server — infrastructure extensions for Device Connect.

Device-side logic (DeviceRuntime, DeviceDriver, messaging, types) lives in
``device_connect_edge``. This package adds server-side infrastructure:

Submodules:
    - device_connect_server.security: ACLs, commissioning, credentials
    - device_connect_server.state: State store abstractions (etcd)
    - device_connect_server.registry: Registry service and client
    - device_connect_server.logging: Audit logging framework
    - device_connect_server.devctl: Device control CLI
    - device_connect_server.statectl: State management CLI

Example:
    from device_connect_edge import DeviceRuntime
    from device_connect_edge.drivers import DeviceDriver, rpc

    class CameraDriver(DeviceDriver):
        device_type = "camera"

        @rpc()
        async def capture_image(self, resolution: str = "1080p") -> dict:
            '''Capture an image.'''
            return {"image_b64": "..."}

    device = DeviceRuntime(
        driver=CameraDriver(),
        device_id="camera-001",
        messaging_urls=["nats://localhost:4222"]
    )
    await device.run()
"""
