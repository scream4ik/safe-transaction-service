import time
from collections import OrderedDict
from logging import getLogger
from typing import Any, Dict, Iterable, List

from requests import HTTPError, ConnectionError

from gnosis.eth import EthereumClient

from ..models import EthereumEvent, EthereumTx, SafeContract
from .ethereum_indexer import EthereumIndexer

logger = getLogger(__name__)


class Erc20EventsIndexerProvider:
    def __new__(cls):
        if not hasattr(cls, 'instance'):
            from django.conf import settings
            cls.instance = Erc20EventsIndexer(EthereumClient(settings.ETHEREUM_NODE_URL))
        return cls.instance

    @classmethod
    def del_singleton(cls):
        if hasattr(cls, "instance"):
            del cls.instance


class Erc20EventsIndexer(EthereumIndexer):
    """
    Indexes ERC20 and ERC721 `Transfer` Event (as ERC721 has the same topic)
    """

    def __init__(self, ethereum_client: EthereumClient, block_process_limit: int = 10000,
                 updated_blocks_behind: int = 300, query_chunk_size: int = 500):
        super().__init__(ethereum_client,
                         block_process_limit=block_process_limit,
                         updated_blocks_behind=updated_blocks_behind,
                         query_chunk_size=query_chunk_size)

    @property
    def database_model(self):
        return SafeContract

    @property
    def database_field(self):
        return 'erc20_block_number'

    def find_relevant_elements(self, addresses: List[str], from_block_number: int,
                               to_block_number: int) -> List[Dict[str, Any]]:
        """
        Search for tx hashes with erc20 transfer events (`from` and `to`) of a `safe_address`
        :param addresses:
        :param from_block_number: Starting block number
        :param to_block_number: Ending block number
        :return: Tx hashes of txs with relevant erc20 transfer events for the `addresses`
        """
        logger.debug('Searching for erc20 txs from block-number=%d to block-number=%d - Safes=%s',
                     from_block_number, to_block_number, addresses)

        # Optimize block process limit
        # Check that we are processing the `block_process_limit`, if not, measures are not valid
        if (to_block_number - from_block_number) == self.block_process_limit:
            start = time.time()
        else:
            start = None

        # It will get erc721 events, as `topic` is the same
        try:
            erc20_transfer_events = self.ethereum_client.erc20.get_total_transfer_history(addresses,
                                                                                          from_block=from_block_number,
                                                                                          to_block=to_block_number)
        except (HTTPError, ConnectionError):
            self.block_process_limit = self.initial_block_process_limit  # Set back to default

        if start:
            end = time.time()
            time_diff = end - start
            if time_diff > 30:
                self.block_process_limit //= 2
                logger.info('ERC20 block_process_limit halved to %d', self.block_process_limit)
            if time_diff > 10:
                self.block_process_limit -= 10000
                logger.info('ERC20 block_process_limit decreased to %d', self.block_process_limit)
            elif time_diff < 2:
                self.block_process_limit *= 2
                logger.info('ERC20 block_process_limit duplicated to %d', self.block_process_limit)
            elif time_diff < 5:
                self.block_process_limit += 10000
                logger.info('ERC20 block_process_limit increased to %d', self.block_process_limit)

        # Log INFO if erc events found, DEBUG otherwise
        logger_fn = logger.info if erc20_transfer_events else logger.debug
        logger_fn('Found %d relevant erc20 txs between block-number=%d and block-number=%d. Safes=%s',
                  len(erc20_transfer_events), from_block_number, to_block_number, addresses)

        return erc20_transfer_events

    def process_elements(self, events: Iterable[Dict[str, Any]]) -> List[EthereumEvent]:
        """
        Process all events found by `find_relevant_elements`
        :param events: Events to store in database
        :return: List of `EthereumEvent` already stored in database
        """
        tx_hashes = OrderedDict.fromkeys([event['transactionHash'] for event in events]).keys()
        ethereum_txs = EthereumTx.objects.create_or_update_from_tx_hashes(tx_hashes)
        ethereum_events = [EthereumEvent.objects.from_decoded_event(event) for event in events]
        return EthereumEvent.objects.bulk_create(ethereum_events, ignore_conflicts=True)