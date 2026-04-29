FROM eclipse-temurin:21-jdk

# 1. Install EVERYTHING needed for Java Native Interface & Tesseract
RUN apt-get update && apt-get install -y \
    python3 python3-pip tesseract-ocr tesseract-ocr-eng \
    libtesseract-dev libleptonica-dev libgomp1 poppler-utils \
    fontconfig libfreetype6 libxext6 libxrender1 libxtst6 \
    wget unzip \
    && apt-get clean

WORKDIR /app

# 2. DOWNLOAD from Google Drive (Handles the large file warning)
# Replace FILE_ID_HERE with your actual Google Drive File ID
RUN FILE_ID="1BUi2iHrLju9XMg5hTqYa3OxiNQ4bKmK1" && \
    wget --load-cookies /tmp/cookies.txt "https://docs.google.com/uc?export=download&confirm=$(wget --quiet --save-cookies /tmp/cookies.txt --keep-session-cookies --no-check-certificate 'https://docs.google.com/uc?export=download&id='$FILE_ID -O- | sed -rn 's/.*confirm=([0-9A-Za-z_]+).*/\1\n/p')&id="$FILE_ID -O audiveris_source.zip && \
    rm -rf /tmp/cookies.txt && \
    unzip audiveris_source.zip && \
    rm audiveris_source.zip

# 3. BUILD Audiveris
# If the zip has a folder inside it, make sure the WORKDIR matches
WORKDIR /app/deploy-app/audiveris_source
RUN ./gradlew build -x test
# -x test skips tests to save time/memory on Railway

# 4. Set up your Python app as usual
WORKDIR /app
COPY app.py requirements.txt ./
COPY templates/ ./templates/
RUN pip3 install --break-system-packages -r requirements.txt

# 5. Env Vars we fixed earlier
ENV HOME=/app/audiveris_home
ENV JAVA_OPTS="-Djava.io.tmpdir=/app/tmp"
ENV LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/usr/local/lib
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/tessdata

RUN mkdir -p /app/audiveris_home /app/tmp uploads output

EXPOSE 5000
CMD ["python3", "app.py"]