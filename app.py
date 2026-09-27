import os
import io
import re
import time
import requests
import zipfile
import gc
from math import floor
from queue import Queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, send_file, jsonify, Response
from PIL import Image

app = Flask(__name__)

log_queues = {}

def send_log(session_id, message):
    if session_id and session_id in log_queues:
        log_queues[session_id].put(message)
    print(f"[{session_id}] {message}")

A4_WIDTH_CM = 21.0
A4_HEIGHT_CM = 29.7
DPI = 300
CARD_WIDTH_CM = 6.48
CARD_HEIGHT_CM = 9.19

A4_WIDTH_PX = int(A4_WIDTH_CM / 2.54 * DPI)
A4_HEIGHT_PX = int(A4_HEIGHT_CM / 2.54 * DPI)
CARD_WIDTH_PX = int(CARD_WIDTH_CM / 2.54 * DPI)
CARD_HEIGHT_PX = int(CARD_HEIGHT_CM / 2.54 * DPI)

COLS = floor(A4_WIDTH_PX / CARD_WIDTH_PX)
ROWS = floor(A4_HEIGHT_PX / CARD_HEIGHT_PX)
CARDS_PER_PAGE = COLS * ROWS

def download_single_image(url):
    print(f"➡️ 準備下載: {url}")
    if not url or "dummy" in url:
        return Image.new("RGB", (CARD_WIDTH_PX, CARD_HEIGHT_PX), "white")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://ws-tcg.com/"
        }
        response = requests.get(url, stream=True, timeout=15, headers=headers)
        if response.status_code == 200:
            image_data = io.BytesIO(response.content)
            return Image.open(image_data).convert("RGB")
    except Exception as e:
        print(f"❌ 下載例外錯誤: {url} | 錯誤: {e}")
    return Image.new("RGB", (CARD_WIDTH_PX, CARD_HEIGHT_PX), "white")

def parallel_download_images(url_list, max_workers=10):
    images = [None] * len(url_list)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {executor.submit(download_single_image, url): i for i, url in enumerate(url_list)}
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            try:
                images[idx] = future.result()
            except:
                images[idx] = Image.new("RGB", (CARD_WIDTH_PX, CARD_HEIGHT_PX), "white")
    return images

def generate_pdf_from_pil_images(pil_images, counts, game_type="WS"):
    final_card_images = []
    for i, img in enumerate(pil_images):
        if i >= len(counts): break
        count = counts[i]
        
        if game_type == "WS" and img.width > img.height:
            img = img.rotate(90, expand=True)
            
        img = img.resize((CARD_WIDTH_PX, CARD_HEIGHT_PX), Image.LANCZOS)
        for _ in range(count):
            # 💡 核心優化：不再使用 .copy()，直接沿用同一個圖片物件，節省巨量記憶體
            final_card_images.append(img)

    pdf_pages = []
    for i in range(0, len(final_card_images), CARDS_PER_PAGE):
        page = Image.new("RGB", (A4_WIDTH_PX, A4_HEIGHT_PX), "white")
        batch = final_card_images[i : i + CARDS_PER_PAGE]
        for idx, card_img in enumerate(batch):
            row = idx // COLS
            col = idx % COLS
            x = col * CARD_WIDTH_PX
            y = row * CARD_HEIGHT_PX
            page.paste(card_img, (x, y))
        pdf_pages.append(page)
        
    final_card_images.clear()

    pdf_buffer = io.BytesIO()
    if pdf_pages:
        pdf_pages[0].save(pdf_buffer, format="PDF", save_all=True, append_images=pdf_pages[1:])
    else:
        Image.new("RGB", (A4_WIDTH_PX, A4_HEIGHT_PX), "white").save(pdf_buffer, format="PDF")
    
    pdf_pages.clear()
    pdf_buffer.seek(0)
    return pdf_buffer

def generate_ws_official_url(card_code):
    match = re.match(r"^([A-Za-z0-9]+)/([A-Za-z0-9]+)-([A-Za-z0-9]+)", card_code)
    if not match: return None
    prefix, series, number = match.groups()
    prefix, series, number = prefix.lower(), series.lower(), number.lower()
    folder1 = prefix[0]
    folder2 = f"{prefix}_{series}"
    return f"https://ws-tcg.com/wordpress/wp-content/images/cardlist/{folder1}/{folder2}/{prefix}_{series}_{number}.png"

def process_ws_logic(url, session_id=None):
    send_log(session_id, "WS: 正在使用無頭 API 解析貓罐子牌組資料...")
    try:
        match = re.search(r'deck/([a-zA-Z0-9]+)', url)
        if not match: return None, None
        
        api_url = f"https://api.bottleneko.app/decks/{match.group(1)}"
        response = requests.get(api_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if response.status_code != 200: return None, None
            
        card_counts = {}
        for card in response.json().get("cards", []):
            card_id = card.get("id")
            if card_id: card_counts[card_id] = card_counts.get(card_id, 0) + 1
                
        img_urls, counts = [], []
        for code, count in card_counts.items():
            img_url = generate_ws_official_url(code)
            img_urls.append(img_url)
            counts.append(count)

        return img_urls, counts
    except Exception as e:
        send_log(session_id, f"WS 爬蟲錯誤: {e}")
        return None, None

def process_ua_logic(url, session_id=None):
    send_log(session_id, "UA: 正在使用無頭 API 解析組牌器網址...")
    try:
        version = re.search(r"Version=([A-Z0-9]+)", url).group(1) if re.search(r"Version=([A-Z0-9]+)", url) else "未知"
        card_entries = url.split("Deck=")[-1].split("|")
        
        img_urls, counts = [], []
        for entry in card_entries:
            if not entry: continue
            match = re.match(r"(\d)([A-Z]+)(\d*[A-Z]*)_(\d{4})(_\d)?", entry)
            if match:
                count, expansion, number, suffix = int(match.group(1)), match.group(2) + match.group(3), match.group(4), match.group(5)
                bandai_suffix = f"_p{int(suffix.replace('_', '')) - 1}" if suffix and int(suffix.replace('_', '')) - 1 > 0 else ""
                filename = f"{expansion}_{version}-{number[0]}-{number[1:]}{bandai_suffix}.png"
                img_urls.append(f"https://www.unionarena-tcg.com/jp/images/cardlist/card/{filename}")
                counts.append(count)
        return img_urls, counts
    except Exception as e:
        send_log(session_id, f"UA 解析錯誤: {e}")
        return None, None

def process_opcg_logic(raw_text, session_id=None):
    send_log(session_id, "OPCG: 正在解析純文字牌表...")
    try:
        matches = re.findall(r'(\d+)\s*[xX*]\s*([A-Za-z0-9-_]+)', raw_text)
        if not matches: return None, None
            
        base_url = "https://asia-tc.onepiece-cardgame.com/images/cardlist/card"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://asia-tc.onepiece-cardgame.com/"}

        def resolve_highest_version(code):
            code = code.upper().strip()
            if "_P" in code or "_p" in code: return code.replace("_P", "_p")
            for suffix in ["_p2", "_p1"]:
                try:
                    if requests.head(f"{base_url}/{code}{suffix}.png", headers=headers, timeout=5).status_code == 200:
                        return f"{code}{suffix}"
                except: pass
            return code

        unique_codes = list(set([code for qty, code in matches]))
        resolved_codes = {}
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_code = {executor.submit(resolve_highest_version, code): code for code in unique_codes}
            for future in as_completed(future_to_code):
                resolved_codes[future_to_code[future]] = future.result()

        img_urls, counts = [], []
        for qty, code in matches:
            img_urls.append(f"{base_url}/{resolved_codes[code]}.png")
            counts.append(int(qty))
            
        return img_urls, counts
    except Exception as e:
        send_log(session_id, f"OPCG 解析錯誤: {e}")
        return None, None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/stream_logs/<session_id>')
def stream_logs(session_id):
    def event_stream():
        if session_id not in log_queues: log_queues[session_id] = Queue()
        while True:
            message = log_queues[session_id].get()
            if message == "DONE": break
            yield f"data: {message}\n\n"
    return Response(event_stream(), mimetype="text/event-stream")

@app.route('/process', methods=['POST'])
def process():
    data = request.json
    raw_urls = data.get('url', '').strip()
    session_id = data.get('session_id')
    need_zip = data.get('need_zip', False)
    
    if not raw_urls: return jsonify({'error': '請提供內容'}), 400

    raw_lines = [u.strip() for u in raw_urls.split('\n') if u.strip()]
    url_list, opcg_buffer = [], []
    for line in raw_lines:
        lower_line = line.lower()
        if line.startswith("http") or "bottleneko" in lower_line or "unionarena" in lower_line or "rugiacreation" in lower_line:
            if opcg_buffer: url_list.append("\n".join(opcg_buffer)); opcg_buffer = []
            url_list.append(line)
        elif re.search(r'\d+\s*[xX*]\s*[A-Za-z0-9-_]+', line):
            opcg_buffer.append(line)
        else:
            if opcg_buffer: opcg_buffer.append(line)
            else: url_list.append(line)
    if opcg_buffer: url_list.append("\n".join(opcg_buffer))

    output_as_zip = (len(url_list) > 1) or need_zip
    master_zip_buffer = io.BytesIO() if output_as_zip else None
    master_zip = zipfile.ZipFile(master_zip_buffer, 'w', zipfile.ZIP_DEFLATED) if output_as_zip else None
    single_pdf_buffer = None
    single_game_type = "WS"
    
    try:
        for index, url in enumerate(url_list, start=1):
            lower_input = url.lower()
            if "unionarena" in lower_input or "rugiacreation" in lower_input: game_type = "UA"; img_urls, counts = process_ua_logic(url, session_id)
            elif "bottleneko" in lower_input or ".app" in lower_input: game_type = "WS"; img_urls, counts = process_ws_logic(url, session_id)
            elif "xop" in lower_input or "xst" in lower_input or "xeb" in lower_input or "xprb" in lower_input or "x" in lower_input: game_type = "OPCG"; img_urls, counts = process_opcg_logic(url, session_id)
            else: continue
                
            if not img_urls or not counts: continue

            pil_images = parallel_download_images(img_urls, max_workers=10)
            pdf_buffer = generate_pdf_from_pil_images(pil_images, counts, game_type)
            
            if output_as_zip:
                master_zip.writestr(f"Deck_{index}_{game_type}.pdf", pdf_buffer.getvalue())
                if need_zip:
                    for i, img in enumerate(pil_images):
                        img_byte_arr = io.BytesIO()
                        img.save(img_byte_arr, format='JPEG', quality=85) # 💡 調降畫質至 85 減輕 ZIP 負擔
                        master_zip.writestr(f"Deck_{index}_images/{i+1:02d}.jpg", img_byte_arr.getvalue())
                        img_byte_arr.close()
            else:
                single_pdf_buffer = pdf_buffer
                single_game_type = game_type
            
            # 💡 核心優化：手動清空迴圈殘留變數，並觸發垃圾回收
            del pil_images
            del pdf_buffer
            gc.collect()

        if output_as_zip:
            master_zip.close()
            master_zip_buffer.seek(0)
            return send_file(master_zip_buffer, as_attachment=True, download_name='Batch_Decks.zip', mimetype='application/zip')
        else:
            if single_pdf_buffer:
                single_pdf_buffer.seek(0)
                return send_file(single_pdf_buffer, as_attachment=True, download_name=f'{single_game_type}_Deck.pdf', mimetype='application/pdf')
            return jsonify({'error': '所有連結皆解析失敗'}), 400

    except Exception as e:
        send_log(session_id, f"嚴重錯誤: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        if session_id in log_queues: log_queues[session_id].put("DONE")
        gc.collect()

if __name__ == '__main__':
    from waitress import serve
    port = int(os.environ.get("PORT", 5000))
    print(f"⚡ API 啟動中... Port: {port}")
    serve(app, host='0.0.0.0', port=port)
