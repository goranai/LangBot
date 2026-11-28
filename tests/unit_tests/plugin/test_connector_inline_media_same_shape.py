"""Inline media that look alike but differ in bytes must each get their own bytes back (Slack photos, DingTalk pictures)."""

from __future__ import annotations

import json
import os

import pytest

import langbot_plugin.api.entities.builtin.platform.message as platform_message
from langbot_plugin.api.entities.builtin.provider import session as provider_session

from langbot.libs.slack_api.slackevent import SlackEvent
from langbot.pkg.platform.adapters.slack import message_converter
from langbot.pkg.plugin.connector import _restore_inline_media, _shed_oversized_inline_media
from tests.factories.message import _base_query, friend_message_event, mock_adapter
from tests.unit_tests.plugin.test_connector_event_inline_media import _emit


def _two_images_query(size: int):
    # A Slack message with two photos: Image(base64=...) with no id, url or name,
    # exactly what slack/message_converter.py builds.
    first = 'data:image/png;base64,' + 'A' * size
    second = 'data:image/png;base64,' + 'B' * size
    chain = platform_message.MessageChain(
        [
            platform_message.Plain(text='two photos'),
            platform_message.Image(base64=first),
            platform_message.Image(base64=second),
        ]
    )
    query = _base_query(
        message_chain=chain,
        message_event=friend_message_event(chain, 12345),
        launcher_type=provider_session.LauncherTypes.PERSON,
        launcher_id=12345,
        sender_id=12345,
        adapter=mock_adapter(),
    )
    return query, first, second


@pytest.mark.asyncio
async def test_two_images_without_ids_keep_their_own_bytes_after_shedding():
    query, first, second = _two_images_query(4 * 1024 * 1024)

    connector, sent, returned = await _emit(query)

    connector.ap.logger.warning.assert_called_once()  # media was shed, so the restore path ran
    shed_images = [c['base64'] for c in sent[0]['event']['message_chain'] if c['type'] == 'Image']
    assert shed_images.count('') == 1  # one photo is enough to fit the frame
    assert [c.base64 for c in returned.event.message_chain if c.type == 'Image'] == [first, second]


def test_shed_keeps_same_shape_components_apart_and_restores_in_order():
    def payload():
        return {
            'a': [{'type': 'Image', 'base64': 'x' * 40}, {'type': 'Image', 'base64': 'y' * 30}],
            'b': [{'type': 'Image', 'base64': 'x' * 40}, {'type': 'Image', 'base64': 'y' * 30}],
        }

    original = payload()
    shed = payload()
    cleared = _shed_oversized_inline_media(shed, budget=0)
    assert all(not node['base64'] for chain in shed.values() for node in chain)

    returned = json.loads(json.dumps(shed))
    _restore_inline_media(returned, cleared)
    assert returned == original


def test_shed_only_the_largest_copies_and_restore_leaves_the_rest():
    payload = {
        'a': [{'type': 'Image', 'base64': 'x' * 40}, {'type': 'Image', 'base64': 'y' * 30}],
        'b': [{'type': 'Image', 'base64': 'x' * 40}, {'type': 'Image', 'base64': 'y' * 30}],
    }
    cleared = _shed_oversized_inline_media(payload, budget=60)

    assert [n['base64'] for n in payload['a']] == ['', 'y' * 30]
    assert [n['base64'] for n in payload['b']] == ['', 'y' * 30]

    returned = json.loads(json.dumps(payload))
    _restore_inline_media(returned, cleared)
    assert [n['base64'] for n in returned['a']] == ['x' * 40, 'y' * 30]
    assert [n['base64'] for n in returned['b']] == ['x' * 40, 'y' * 30]


@pytest.mark.asyncio
async def test_slack_message_with_two_photos_keeps_both_photos(monkeypatch):
    photos = {
        'https://files.slack.com/files-pri/T1-F1/one.jpg': os.urandom(2_600_000),
        'https://files.slack.com/files-pri/T1-F2/two.jpg': os.urandom(2_600_000),
    }

    async def download_media(url, token):
        return photos[url], 'image/jpeg'

    monkeypatch.setattr(message_converter, 'download_media', download_media)
    event = SlackEvent(
        {
            'event': {
                'type': 'message',
                'channel_type': 'im',
                'ts': '1700000000.000100',
                'text': 'two photos',
                'files': [
                    {
                        'id': 'F1',
                        'mimetype': 'image/jpeg',
                        'url_private_download': 'https://files.slack.com/files-pri/T1-F1/one.jpg',
                    },
                    {
                        'id': 'F2',
                        'mimetype': 'image/jpeg',
                        'url_private_download': 'https://files.slack.com/files-pri/T1-F2/two.jpg',
                    },
                ],
            }
        }
    )
    chain = await message_converter.SlackMessageConverter.target2yiri(event, 'xoxb-test')
    sent_images = [c.base64 for c in chain if c.type == 'Image']
    assert len(sent_images) == 2 and sent_images[0] != sent_images[1]

    query = _base_query(
        message_chain=chain,
        message_event=friend_message_event(chain, 12345),
        launcher_type=provider_session.LauncherTypes.PERSON,
        launcher_id=12345,
        sender_id=12345,
        adapter=mock_adapter(),
    )
    connector, sent, returned = await _emit(query)

    connector.ap.logger.warning.assert_called_once()
    assert [c.base64 for c in returned.event.message_chain if c.type == 'Image'] == sent_images
