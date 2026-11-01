FROM python:3.12-slim-bookworm

WORKDIR /app

RUN pip install --no-cache-dir yt-dlp

COPY server.py understand.py ./

EXPOSE 8790

CMD ["python", "-u", "server.py"]
