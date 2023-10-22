import json
import logging
import os
import re
import time
from typing import Any
from datetime import timedelta

from dotenv import load_dotenv

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_bolt.adapter.aws_lambda import SlackRequestHandler

from langchain.chat_models import ChatOpenAI
from langchain.callbacks.base import BaseCallbackHandler
from langchain.schema import HumanMessage, LLMResult, SystemMessage
from langchain.memory import MomentoChatMessageHistory, ConversationBufferMemory
from langchain.chains import RetrievalQA, ConversationalRetrievalChain

from add_document import initialize_vectorstore

CHAT_UPDATE_INTERVAL_SEC = 1

load_dotenv()

# ログ
SlackRequestHandler.clear_all_log_handlers()
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ボットトークンを使ってアプリを初期化します
app = App(
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
    token=os.environ.get("SLACK_BOT_TOKEN"),
    # ???
    process_before_response=True,
)

class SlackStreamingCallbackHandler(BaseCallbackHandler):
    last_send_time = time.time()
    message=""
    
    def __init__(self, channel, ts):
        self.channel = channel
        self.ts = ts
        self.interval = CHAT_UPDATE_INTERVAL_SEC
        # 投稿を更新した累計回数カウンタ
        self.update_count = 0
        
    def on_llm_new_token(self, token:str, **kwargs) -> None:
        self.message += token
        
        now = time.time()
        if now -self.last_send_time > self.interval:
            app.client.chat_update(
                channel=self.channel, 
                ts=self.ts, 
                text=f"{self.message}\n\nTyping",
            )
            self.last_send_time = now
            self.update_count += 1
            
            # update_countが現在の更新間隔*10より大きくなるたびに更新感覚を2倍にする
            if self.update_count / 10 > self.interval:
                self.interval = self.interval * 2
    
    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> Any:
        message_context = "OpenAI APIで生成される情報は不正確または不適切な場合がありますが、当社の見解を述べるものではありません。"
        message_blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": self.message,
                },
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": message_context,
                    }
                ],
            },
        ]
        app.client.chat_update(
            channel=self.channel, 
            ts=self.ts, 
            text=self.message,
            blocks=message_blocks,
        )

# @app.event("app_mention")
def handle_mention(event, say):
    channel = event["channel"]
    thread_ts = event["ts"]
    message = re.sub("<@.*>", "", event["text"])
    
    # 投稿の先頭(=Momentoキー)を示す：初回はevent["ts"],2回目以降はevent["thread_ts"]
    id_ts = event["ts"]
    if "thread_ts" in event:
        id_ts = event["thread_ts"]
    
    # 回答処理中のための文字
    result = say("\n\nTyping...", thread_ts=thread_ts)
    ts = result["ts"]
    
    history = MomentoChatMessageHistory.from_client_params(
        id_ts,
        os.environ["MOMENTO_CACHE"],
        timedelta(hours=int(os.environ["MOMENTO_TTL"])),
    )
    memory = ConversationBufferMemory(
        chat_memory=history,
        memory_key="chat_history",
        return_messages=True
    )
    
    vectorstore = initialize_vectorstore()
    
    callback = SlackStreamingCallbackHandler(channel=channel, ts=ts)
    llm = ChatOpenAI(
        model_name = os.environ["OPENAI_API_MODEL"],
        temperature= os.environ["OPENAI_API_TEMPERATURE"],
        streaming=True,
        callbacks=[callback],
    )
    condense_question_llm = ChatOpenAI(
        model_name=os.environ["OPENAI_API_MODEL"],
        temperature=os.environ["OPENAI_API_TEMPERATURE"],
    )
    
    qa_chain = ConversationalRetrievalChain.from_llm(
        llm=llm, 
        retriever=vectorstore.as_retriever(),
        memory=memory,
        condense_question_llm=condense_question_llm,
    )
    
    qa_chain.run(message)
        
        
def just_ack(ack):
    ack()
    
app.event("app_mention")(ack=just_ack, lazy=[handle_mention])

# ソケットモードハンドラーを使ってアプリを起動します
if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()

def handler(event, context):
    """Lambda関数のエントリーポイント"""
    logger.info("handler called")
    header = event["headers"]
    logger.info(json.dumps(header))
    
    if "x-slack-retry-num" in header:
        logger.info("SKIP > x-slack-retry-num: %s", header["x-slack-retry-num"])
        return 200
        
    # AWS Lambda 環境のリクエスト情報をappが処理できるよう変換してくれるアダプター
    slack_handler = SlackRequestHandler(app=app)
    # 応答はそのままAWS Lambdaの戻り値として返せます
    return slack_handler.handle(event, context)