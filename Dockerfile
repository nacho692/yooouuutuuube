FROM python:3.12-slim

# yt-dlp needs a JS runtime to solve YouTube's player challenges.
COPY --from=denoland/deno:bin-2.5.6 /deno /usr/local/bin/deno

RUN useradd --create-home app && python -m venv /venv && chown -R app /venv
USER app
ENV PATH=/venv/bin:$PATH HOST=0.0.0.0
RUN pip install --no-cache-dir "yt-dlp[default]"

WORKDIR /app
COPY index.html serve.py test.mp4 ./
EXPOSE 8731

# YouTube breaks yt-dlp often; pick up the latest release on every start.
CMD ["sh", "-c", "pip install -q --no-cache-dir -U 'yt-dlp[default]' || true; exec python serve.py 8731"]
