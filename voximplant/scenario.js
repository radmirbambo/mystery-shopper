/**
 * Voximplant scenario «mystery-shopper inbound».
 *
 * Роль агента: пациент, который оставил заявку и ждёт перезвона.
 * Цель за 40 секунд: узнать, какая клиника звонит (если Caller ID не совпал),
 * зафиксировать факт и время перезвона, вежливо закончить. НИКОГДА не записываться на приём.
 *
 * Настройка в панели Voximplant:
 *  1. Applications → создать приложение, привязать к нему купленный номер (RU, Москва).
 *  2. Scenarios → вставить этот файл. В Routing rule привязать сценарий к номеру.
 *  3. Заменить BACKEND и TOKEN ниже. Бэкенд должен быть доступен по https.
 *  4. Имена сверены с VoxEngine typings 7.62.0 (28.08.2026): VoiceList.Yandex.ru_RU_* больше нет,
 *     русские голоса Яндекса — VoiceList.Yandex.Neural.* (или VoiceList.YandexV3.*); ASR — ASRProfileList.Yandex.ru_RU.
 *     call.say принимает { voice }, а не { language }; headers в Net.httpRequestAsync — объект, id звонка — call.id().
 */

require(Modules.ASR);

const BACKEND = "https://YOUR-HOST/";          // адрес app.py
const TOKEN = "change-me";                      // тот же, что MS_TOKEN у бэкенда
const VOICE = VoiceList.Yandex.Neural.ru_RU_alena;   // голос (женский, нейросетевой Яндекс)
const ASR_PROFILE = ASRProfileList.Yandex.ru_RU;
const MAX_CALL_SEC = 75;                        // страховка: дольше не разговариваем

let call = null;
let asr = null;
let callId = "";
let transcript = [];
let clinicSaid = "";                            // ответ на прямой вопрос «из какой вы клиники»
let firstHeard = "";                            // первая реплика клиники — запасной вариант для матчинга
let matchedName = null;
let startedAt = 0;
let turn = 0;                                   // 0 — приветствие, 1 — спросили клинику, 2 — прощаемся

function post(path, data) {
  return Net.httpRequestAsync(BACKEND + path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Token": TOKEN },
    postData: JSON.stringify(data),
  });
}

function say(text) {
  transcript.push("Я: " + text);
  call.say(text, { voice: VOICE });
}

function listen() {
  if (asr) asr.stop();
  asr = VoxEngine.createASR({ profile: ASR_PROFILE, singleUtterance: true });
  asr.addEventListener(ASREvents.Result, (e) => {
    const heard = (e.text || "").trim();
    transcript.push("Клиника: " + heard);
    Logger.write("ASR: " + heard);
    onHeard(heard);
  });
  // тишина 6 секунд — считаем, что реплика закончилась
  asr.addEventListener(ASREvents.SpeechCaptured, () => {});
  call.sendMediaTo(asr);
}

function onHeard(heard) {
  if (turn === 0) {
    firstHeard = heard;
    // Если Caller ID совпал — подтверждаем и сразу закрываем.
    if (matchedName) {
      turn = 2;
      say("Да, оставлял. Спасибо, что перезвонили. Я пока сравниваю несколько клиник, наберу вам сам, когда определюсь. Всего доброго.");
      return;
    }
    // Иначе просим назвать клинику — один раз.
    turn = 1;
    say("Да, оставлял, но заявок у меня несколько. Подскажите, пожалуйста, из какой вы клиники?");
    return;
  }
  if (turn === 1) {
    clinicSaid = heard;
    turn = 2;
    say("Понял, спасибо. Я пока сравниваю варианты и перезвоню сам. Всего доброго.");
    return;
  }
}

function finish() {
  const duration = Math.round((Date.now() - startedAt) / 1000);
  if (asr) { try { asr.stop(); } catch (err) {} }
  post("call/end", {
    call_id: callId,
    transcript: transcript.join("\n"),
    clinic_said: clinicSaid || firstHeard,      // если до вопроса не дошло — матчим по первой реплике
    duration_sec: duration,
  }).then(() => VoxEngine.terminate(), () => VoxEngine.terminate());
}

VoxEngine.addEventListener(AppEvents.CallAlerting, (e) => {
  call = e.call;
  callId = (call.id && call.id()) || String(Date.now());
  const caller = e.callerid || "anonymous";
  startedAt = Date.now();
  Logger.write("Inbound from " + caller);

  call.addEventListener(CallEvents.Connected, () => {
    // Сначала спрашиваем бэкенд, знаем ли мы этот номер.
    post("call/start", { call_id: callId, caller: caller, ts: new Date().toISOString() })
      .then((res) => {
        try {
          const body = JSON.parse(res.text || "{}");
          if (body.matched) matchedName = body.clinic_name;
        } catch (err) {}
        say("Алло, да, слушаю.");
      }, () => say("Алло, да, слушаю."));
  });

  // после каждой нашей реплики: либо слушаем, либо кладём трубку
  call.addEventListener(CallEvents.PlaybackFinished, () => {
    if (turn === 2) {
      setTimeout(() => call.hangup(), 800);
    } else {
      listen();
    }
  });

  call.addEventListener(CallEvents.Disconnected, () => finish());
  call.addEventListener(CallEvents.Failed, () => finish());

  setTimeout(() => { if (call) call.hangup(); }, MAX_CALL_SEC * 1000);

  call.answer();
});
