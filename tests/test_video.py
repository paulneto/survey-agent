import json
import os

import pytest

from survey_agent.media import youtube_id, youtube_embed
from survey_agent.engine.runtime import SurveyRuntime

SURVEY = os.path.join(os.path.dirname(__file__), "..", "surveys", "streaming-usage.survey.json")


def test_youtube_embed_variants():
    for url in ["https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "https://youtu.be/dQw4w9WgXcQ",
                "https://www.youtube.com/embed/dQw4w9WgXcQ",
                "https://youtube.com/shorts/dQw4w9WgXcQ"]:
        assert youtube_id(url) == "dQw4w9WgXcQ"
        assert youtube_embed(url) == "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
    assert youtube_embed("https://vimeo.com/123") is None   # non-youtube -> plain <video>


@pytest.fixture
def rt():
    with open(SURVEY) as f:
        return SurveyRuntime(json.load(f))


def test_video_is_display_only_not_required(rt):
    # p_intro has a video stimulus + a required consent radio; submitting only the consent must pass
    qs = rt.initial_quota_state()
    s = rt.new_session("t:vid", {"uid": "1", "rid": "2", "group": "demo"})
    rt.start(s, qs)
    step = rt.submit(s, "p_intro", {"q_consent": "yes"}, qs)   # no answer for q_intro_video
    assert "errors" not in step                                # video isn't validated as an input
    assert step["page"]["page_id"] == "p_screen"


def test_video_media_renders_through(rt):
    s = rt.new_session("t:vid2", {"uid": "1", "rid": "2", "group": "demo"})
    page = rt.start(s, qs := rt.initial_quota_state())["page"]
    vid = next(q for q in page["questions"] if q["id"] == "q_intro_video")
    assert vid["type"] == "video"
    assert vid["media"]["url"].endswith("aqz-KE-bpKQ")
    assert youtube_embed(vid["media"]["url"]).startswith("https://www.youtube-nocookie.com/embed/")
