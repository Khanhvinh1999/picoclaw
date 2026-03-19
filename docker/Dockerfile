# ============================================================
# Extend upstream picoclaw with gws CLI + custom skills
# Build context: docker/
# ============================================================
FROM docker.io/sipeed/picoclaw:latest

USER root

# Install gws CLI (Google Workspace) — persists across container restarts
RUN apk add --no-cache nodejs npm && \
    npm install -g @googleworkspace/cli --force && \
    npm cache clean --force

# Prepare gws config directory for the picoclaw user
RUN mkdir -p /home/picoclaw/.config/gws && \
    chown -R picoclaw:picoclaw /home/picoclaw/.config

# Copy custom skills into the image
COPY skills/ /home/picoclaw/.picoclaw/workspace/skills/
RUN chown -R picoclaw:picoclaw /home/picoclaw/.picoclaw/workspace/skills

USER picoclaw

ENTRYPOINT ["picoclaw"]
CMD ["gateway"]
