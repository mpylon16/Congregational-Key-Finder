FROM eclipse-temurin:21-jdk

# Install Python, Tesseract, and other dependencies
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    tesseract-ocr \
    tesseract-ocr-eng \
    poppler-utils \
    && apt-get clean

WORKDIR /app

# Copy pre-compiled Audiveris
COPY audiveris_compiled/ ./audiveris/

# Make Audiveris executable
RUN chmod +x ./audiveris/bin/Audiveris

# Copy Flask app
COPY app.py .
COPY requirements.txt .
COPY templates/ ./templates/

# Create necessary directories
RUN mkdir -p uploads output

# Install Python dependencies
RUN pip3 install --break-system-packages -r requirements.txt

# Environment variables
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata

EXPOSE 5000

CMD ["python3", "app.py"]