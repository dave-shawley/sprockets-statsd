import asyncio
import io


class StatsdServer(asyncio.Protocol):
    def __init__(self):
        self.service = None
        self.host = '127.0.0.1'
        self.port = 0
        self.connections_made = 0
        self.connections_lost = 0
        self.message_counter = 0

        self.metrics = []
        self.running = asyncio.Event()
        self.client_connected = asyncio.Semaphore(value=0)
        self.message_received = asyncio.Semaphore(value=0)
        self.transports: list[asyncio.Transport] = []

        self._buffer = io.BytesIO()

    async def run(self):
        await self._reset()

        loop = asyncio.get_running_loop()
        self.service = await loop.create_server(lambda: self,
                                                self.host,
                                                self.port,
                                                reuse_port=True)
        listening_sock = self.service.sockets[0]
        self.host, self.port = listening_sock.getsockname()
        self.running.set()
        try:
            await self.service.serve_forever()
            self.running.clear()
        except asyncio.CancelledError:
            self.close()
            await self.service.wait_closed()
        except Exception as error:
            raise error

    def close(self):
        self.running.clear()
        self.service.close()
        for connected_client in self.transports:
            connected_client.close()
        self.transports.clear()

    async def wait_running(self):
        await self.running.wait()

    async def wait_closed(self):
        if self.service.is_serving():
            self.close()
            await self.service.wait_closed()
        while self.running.is_set():
            await asyncio.sleep(0.1)

    def connection_made(self, transport: asyncio.Transport):
        self.client_connected.release()
        self.connections_made += 1
        self.transports.append(transport)

    def connection_lost(self, exc) -> None:
        self.connections_lost += 1

    def data_received(self, data: bytes):
        self._buffer.write(data)
        buf = self._buffer.getvalue()
        if b'\n' in buf:
            buf_complete = buf[-1] == ord('\n')
            if not buf_complete:
                offset = buf.rfind(b'\n')
                self._buffer = io.BytesIO(buf[offset:])
                buf = buf[:offset]
            else:
                self._buffer = io.BytesIO()
                buf = buf[:-1]

            for metric in buf.split(b'\n'):
                self.metrics.append(metric)
                self.message_received.release()
                self.message_counter += 1

    async def _reset(self):
        self._buffer = io.BytesIO()
        self.connections_made = 0
        self.connections_lost = 0
        self.message_counter = 0
        self.metrics.clear()
        for transport in self.transports:
            transport.close()
        self.transports.clear()

        self.running.clear()
        await self._drain_semaphore(self.client_connected)
        await self._drain_semaphore(self.message_received)

    @staticmethod
    async def _drain_semaphore(semaphore: asyncio.Semaphore):
        while not semaphore.locked():
            try:
                await asyncio.wait_for(semaphore.acquire(), 0.1)
            except asyncio.TimeoutError:
                break
