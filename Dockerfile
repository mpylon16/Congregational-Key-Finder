FROM eclipse-temurin:21-jdk

# 1. Install EVERYTHING needed for Java Native Interface & Tesseract
RUN apt-get update && apt-get install -y \
    python3 python3-pip tesseract-ocr tesseract-ocr-eng \
    libtesseract-dev libleptonica-dev libgomp1 poppler-utils \
    fontconfig libfreetype6 libxext6 libxrender1 libxtst6 \
    wget unzip \
    && apt-get clean

WORKDIR /app

# Simpler download for Dropbox
RUN wget -O audiveris_source.zip "https://www.dropbox.com/scl/fi/ehql5rgigwea1q7cwymsr/audiveris_source.zip?rlkey=m5rol41patcos7u2fxsp2mttb&st=pzt2jb81&dl=1" && \
    unzip audiveris_source.zip && \
    rm audiveris_source.zip

# 3. BUILD Audiveris
# Move into the folder and make sure the 'gradlew' file is allowed to run
WORKDIR /app/audiveris_source
RUN chmod +x gradlew
RUN ./gradlew build -x test
# -x test skips tests to save time/memory on Railway

# 4. Set up your Python app as usual
WORKDIR /app
COPY . .
RUN pip3 install --break-system-packages -r requirements.txt

# 5. Env Vars we fixed earlier
ENV HOME=/app/audiveris_home
ENV JAVA_OPTS="-Djava.io.tmpdir=/app/tmp"
ENV LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:/usr/local/lib
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/tessdata

RUN mkdir -p /app/audiveris_home /app/tmp uploads output

EXPOSE 5000
CMD ["python3", "app.py"]
