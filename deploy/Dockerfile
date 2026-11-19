# NepSync のコンテナ（ADR 0013。VM-2 の ~/service/src に送り、compose.yaml から組み立てる）
# 組み立ての元は ~/service/src（= このリポジトリの requirements.txt・work/・deploy/ を git archive で送ったもの）
FROM python:3.14.7-slim-trixie

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 名簿の格子の画像に、日本語の字体が要る（ADR 0014 判断2。nepsync/grid.py の FONT_PATHS）
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

# config.py の ROOT は work/nepsync の2つ上（= /app）。動いている状態は /app/data（compose.yaml で ./data をつなぐ）
COPY work/nepsync /app/work/nepsync
WORKDIR /app/work

CMD ["python", "-W", "ignore::ResourceWarning", "-m", "nepsync", "run"]
