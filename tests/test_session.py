"""Session 会话 id 测试：构造与 /new（reset）时轮换，供自定义请求头 {$session} 使用。"""

import uuid

from smithcode import config
from smithcode.llm.session import Session


def test_construction_rotates_session_id():
    first = config.SESSION_ID
    Session()
    assert config.SESSION_ID != first
    # uuid4().hex：32 位十六进制
    assert len(config.SESSION_ID) == 32
    uuid.UUID(config.SESSION_ID)


def test_reset_rotates_session_id():
    Session()
    before = config.SESSION_ID
    Session().reset()
    assert config.SESSION_ID != before
