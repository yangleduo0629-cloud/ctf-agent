import argparse
import asyncio


async def copy_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(64 * 1024):
            writer.write(chunk)
            await writer.drain()
    finally:
        writer.close()


async def relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
) -> None:
    try:
        target_reader, target_writer = await asyncio.open_connection(target_host, target_port)
    except OSError:
        client_writer.close()
        return

    await asyncio.gather(
        copy_stream(client_reader, target_writer),
        copy_stream(target_reader, client_writer),
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Relay a Windows loopback proxy to the WSL adapter")
    parser.add_argument("--listen-host", required=True)
    parser.add_argument("--listen-port", type=int, default=17890)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=7890)
    args = parser.parse_args()

    server = await asyncio.start_server(
        lambda reader, writer: relay(reader, writer, args.target_host, args.target_port),
        args.listen_host,
        args.listen_port,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
