import asyncio
import logging
from pathlib import Path
import socket
import subprocess
import time

import pytest
import rlp
from eth_utils import (
    decode_hex,
    encode_hex,
    to_text,
)
from eth_hash.auto import keccak

from trinity.protocol.common.context import ChainContext
from trinity.protocol.les.peer import LESPeerPool
from trinity.sync.light.chain import LightChainSyncer
from trinity.sync.light.service import LightPeerChain
from trinity.protocol.les.servers import LightRequestServer

from trinity import constants
from eth import Chain
from eth.constants import GENESIS_BLOCK_NUMBER
from eth.vm.forks.frontier import FrontierVM
from eth.vm.forks.homestead import HomesteadVM
from eth.chains.mainnet import HOMESTEAD_MAINNET_BLOCK
from eth.db.atomic import AtomicDB
from eth.chains.mainnet import MAINNET_GENESIS_HEADER
from web3 import Web3, HTTPProvider

from p2p import ecies
from p2p.kademlia import Node

from tests.trinity.core.integration_test_helpers import (
    FakeAsyncChainDB,
    FakeAsyncMainnetChain,
    FakeAsyncHeaderDB,
    connect_to_peers_loop,
)


@pytest.fixture
def dao_block_number():
    return 1920001

@pytest.fixture
def dao_hex_byte(dao_block_number):
    web3 = Web3(HTTPProvider('https://ethereumclassic.network'))
    hexByte = web3.eth.getBlock(dao_block_number)['hash'].hex()

@pytest.fixture
def enode():
    return "enode://0001b440cd154be17d04e5a9cb0079efd550e4569d2d860419d897152de46032ccf51e250750057e23905f93fa5273e13d9931893c741f03ab3a427ef0e43243@186.195.33.133:55519"

def wait_for_socket(ipc_path, timeout=10):
    start = time.monotonic()
    while time.monotonic() < start + timeout:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(ipc_path))
            sock.settimeout(timeout)
        except (FileNotFoundError, socket.error):
            time.sleep(0.01)
        else:
            break

@pytest.fixture
def geth_ipc_path(geth_datadir):
    return '/home/admin/.ethereum-classic/mainnet/geth.ipc' 

@pytest.fixture
def geth_process(geth_command_arguments):
    proc = subprocess.Popen(
        geth_command_arguments,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
    )
    logging.warning('start geth: %r' % (geth_command_arguments,))
    try:
        yield proc
    finally:
        logging.warning('shutting down geth')
        kill_popen_gracefully(proc, logging.getLogger('tests.trinity.integration.lightchain'))
        output, errors = proc.communicate()
        logging.warning(
            "Geth Process Exited:\n"
            "stdout:{0}\n\n"
            "stderr:{1}\n\n".format(
                to_text(output),
                to_text(errors),
            )
        )

@pytest.mark.asyncio
async def test_etc_node(event_loop, request, dao_hex_byte, dao_block_number, enode):
    remote = Node.from_uri(enode)
    base_db = AtomicDB()
    chaindb = FakeAsyncChainDB(base_db)
    chaindb.persist_header(MAINNET_GENESIS_HEADER)
    headerdb = FakeAsyncHeaderDB(base_db)
    context = ChainContext(
        headerdb=headerdb,
        network_id=constants.MAINNET_NETWORK_ID,
        vm_configuration=(
            (GENESIS_BLOCK_NUMBER, FrontierVM),
            (HOMESTEAD_MAINNET_BLOCK, HomesteadVM),
        ),
    )
    peer_pool = LESPeerPool(
        privkey=ecies.generate_privkey(),
        context=context,
    )
    chain = FakeAsyncMainnetChain(base_db)
    syncer = LightChainSyncer(chain, chaindb, peer_pool)
    syncer.min_peers_to_sync = 1
    peer_chain = LightPeerChain(headerdb, peer_pool)

    server_request_handler = LightRequestServer(headerdb, peer_pool)
    asyncio.ensure_future(peer_pool.run())
    asyncio.ensure_future(connect_to_peers_loop(peer_pool, tuple([remote])))
    asyncio.ensure_future(peer_chain.run())
    asyncio.ensure_future(server_request_handler.run())
    asyncio.ensure_future(syncer.run())
    await asyncio.sleep(0)  # Yield control to give the LightChainSyncer a chance to start

    def finalizer():
        event_loop.run_until_complete(peer_pool.cancel())
        event_loop.run_until_complete(peer_chain.cancel())
        event_loop.run_until_complete(syncer.cancel())
        event_loop.run_until_complete(server_request_handler.cancel())

    request.addfinalizer(finalizer)

    n = dao_block_number

    # Wait for the chain to sync a few headers.
    async def wait_for_header_sync(block_number):
        while headerdb.get_canonical_head().block_number < block_number:
            await asyncio.sleep(0.1)
    await asyncio.wait_for(wait_for_header_sync(n), None)

    header = headerdb.get_canonical_block_header_by_number(n)
    body = await peer_chain.coro_get_block_body_by_hash(header.hash)

    receipts = await peer_chain.coro_get_receipts(header.hash)
    if encode_hex(keccak(rlp.encode(receipts[0]))) == (dao_hex_byte):
        with open('document.csv','a') as fd:
            fd.write(enode)

    peer = peer_pool.highest_td_peer
    head = await peer_chain.coro_get_block_header_by_hash(peer.head_hash)
