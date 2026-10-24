FROM bines-launcher:before-website-auto-20261024
COPY --chown=501:20 common/activity_deliberation.py common/proactive.py common/website.py common/website_watch.py common/website_spool.py common/website_observer.py common/website_perception.py common/website_cdp.py common/visual_change.py /app/common/
COPY --chown=501:20 tools/context.py /app/tools/context.py
COPY --chown=501:20 thingking/src/thinking_model_helper.py /app/thingking/src/thinking_model_helper.py
COPY --chown=501:20 thingking/tests/test_website_observer.py /app/thingking/tests/test_website_observer.py
COPY --chown=501:20 skills/website/SKILL.md /app/skills/website/SKILL.md
ARG BINES_SOURCE_HASH
LABEL io.bines.source-hash="${BINES_SOURCE_HASH}"
