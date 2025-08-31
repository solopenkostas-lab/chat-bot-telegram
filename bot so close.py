from telegram.ext import Application, ContextTypes, JobQueue
import feedparser
from deep_translator import GoogleTranslator
from transformers import pipeline
import hashlib
import logging
import re
import requests
import asyncio
import psutil
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

# Проверка зависимостей
try:
    import psutil
except ImportError:
    print("Установите psutil: pip install psutil")
    exit(1)

# Настройка логирования
logging.basicConfig(
    filename='news_bot.log',
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class NewsBot:
    def __init__(self, token: str, channel_id: str):
        self.token = token
        self.channel_id = channel_id
        self.feeds = [
            'https://lenta.ru/rss/news',
            'https://www.vedomosti.ru/rss/news'
        ]
        self.news_processor = EnhancedNewsProcessor()
        self.application = Application.builder().token(token).build()
        
        self.health_stats = {
            'sent': 0,
            'errors': 0,
            'last_check': datetime.now()
        }

    async def fetch_news(self, context: ContextTypes.DEFAULT_TYPE):
        """Основной метод получения и обработки новостей"""
        try:
            for feed_url in self.feeds:
                feed = feedparser.parse(feed_url)
                
                if feed.bozo:
                    logger.error(f"Ошибка RSS: {feed.bozo_exception}")
                    continue
                
                for entry in feed.entries[:5]:
                    message, image = self.news_processor.process_entry(entry)
                    if message:
                        await self._send_message_with_retry(message, image)
                        self.health_stats['sent'] += 1
                        await asyncio.sleep(1)
            
            self.health_stats['last_check'] = datetime.now()
            
        except Exception as e:
            logger.error(f"Ошибка: {str(e)}", exc_info=True)
            self.health_stats['errors'] += 1

    async def _send_message_with_retry(self, text: str, image_url: str = None):
        """Отправка сообщения с 3 попытками"""
        for attempt in range(3):
            try:
                if image_url:
                    await self.application.bot.send_photo(
                        chat_id=self.channel_id,
                        photo=image_url,
                        caption=text,
                        parse_mode='MarkdownV2'
                    )
                else:
                    await self.application.bot.send_message(
                        chat_id=self.channel_id,
                        text=text,
                        parse_mode='MarkdownV2',
                        disable_web_page_preview=True
                    )
                return
            except Exception as e:
                logger.warning(f"Попытка {attempt+1} ошибка: {str(e)}")
                await asyncio.sleep(2 ** attempt)
        
        logger.error("Не удалось отправить сообщение")
        self.health_stats['errors'] += 1

    def start(self):
        """Запуск бота"""
        try:
            # Проверка токена
            response = requests.get(f"https://api.telegram.org/bot{self.token}/getMe")
            if response.status_code != 200:
                logger.critical("Неверный токен бота")
                exit(1)

            # Настройка периодической проверки
            job_queue = self.application.job_queue
            job_queue.run_repeating(
                self.fetch_news, 
                interval=1800,  # 30 минут
                first=10
            )

            # Запуск
            self.application.run_polling()
            logger.info("Бот успешно запущен")

        except Exception as e:
            logger.critical(f"Ошибка запуска: {str(e)}")
            exit(1)

class EnhancedNewsProcessor:
    def __init__(self):
        self.translator = GoogleTranslator(source='auto', target='ru')
        self.summarizer = pipeline("summarization", model="IlyaGusev/rut5_base_sum_gazeta")
        self.cache = set()
        self.last_cache_clean = datetime.now()

    def _clean_cache(self):
        """Очистка кэша каждые 2 часа"""
        if (datetime.now() - self.last_cache_clean).total_seconds() > 7200:
            self.cache.clear()
            self.last_cache_clean = datetime.now()

    def process_entry(self, entry) -> tuple:
        """Обработка RSS-записи"""
        try:
            self._clean_cache()
            
            # Генерация уникального хеша
            entry_hash = hashlib.sha256(entry.link.encode()).hexdigest()
            if entry_hash in self.cache:
                return None, None

            # Проверка даты публикации
            pub_date = datetime(*entry.published_parsed[:6]) if hasattr(entry, 'published_parsed') else None
            if pub_date and (datetime.now() - pub_date).days > 1:
                return None, None

            # Обработка заголовка
            title = self._process_text(entry.title)
            if not title:
                return None, None

            # Обработка контента
            content = self._process_text(getattr(entry, 'description', ''))
            summary = self.summarizer(
                content,
                max_length=150,
                min_length=50,
                do_sample=False
            )[0]['summary_text']

            # Форматирование сообщения
            message = (
                f"📌 *{title}*\n\n"
                f"{summary}\n\n"
                f"[Читать полностью]({entry.link})"
            )

            # Поиск изображения
            image_url = self._find_image(entry)

            self.cache.add(entry_hash)
            return message, image_url

        except Exception as e:
            logger.error(f"Ошибка обработки: {str(e)}")
            return None, None

    def _process_text(self, text: str) -> str:
        """Очистка и перевод текста"""
        clean_text = re.sub(r'<[^>]+>|[\t\r\n]+', ' ', text).strip()
        if not clean_text:
            return ""
            
        try:
            return self.translator.translate(clean_text[:2000])
        except Exception:
            return clean_text[:1000]

    def _find_image(self, entry) -> str:
        """Поиск валидного изображения"""
        for enclosure in getattr(entry, 'enclosures', []):
            if enclosure.type.startswith('image/'):
                return enclosure.href
        return ""

if __name__ == '__main__':
    # Конфигурация
    BOT_TOKEN = '7991018460:AAH7l8b1HbX09YlJgCTiWQRyWlzeHeIYBgM'
    CHANNEL_ID = '@proriv14'

    try:
        bot = NewsBot(BOT_TOKEN, CHANNEL_ID)
        logger.info("Инициализация бота...")
        bot.start()
    except KeyboardInterrupt:
        logger.info("Остановка бота")
    except Exception as e:
        logger.critical(f"ФАТАЛЬНАЯ ОШИБКА: {str(e)}")
        exit(1)
