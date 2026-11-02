FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chmod +x start.sh
ENV DATABASE_URL=sqlite:////data/hoops.db
VOLUME /data
EXPOSE 8000
CMD ["./start.sh"]
