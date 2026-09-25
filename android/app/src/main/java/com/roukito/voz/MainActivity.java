package com.roukito.voz;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.media.AudioManager;
import android.media.ToneGenerator;
import android.os.Bundle;
import android.provider.Settings;
import android.os.Handler;
import android.os.Looper;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.widget.Button;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends Activity {
    private static final String API = "http://192.168.68.100:8790/api/turn";
    private static final String MENU = "¿Qué quieres hacer? Uno, buscar un video. Dos, detener el video. Tres, reproducir el video.";
    private static final int MIC = 1;

    private final String session = UUID.randomUUID().toString();
    private final ExecutorService network = Executors.newSingleThreadExecutor();
    private final Handler main = new Handler(Looper.getMainLooper());

    private Button talk;
    private TextView status;
    private TextToSpeech tts;
    private boolean ttsReady;
    private boolean startWhenReady;
    private SpeechRecognizer recognizer;
    private String phase = "menu";
    private boolean busy;
    private int speakGeneration;
    private ToneGenerator waitingTone;
    private Runnable waitingTick;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        talk = findViewById(R.id.talk);
        status = findViewById(R.id.status);
        talk.setOnClickListener(v -> {
            if (!busy) {
                onTalk();
            }
        });
        talk.post(() -> talk.performAccessibilityAction(android.view.accessibility.AccessibilityNodeInfo.ACTION_ACCESSIBILITY_FOCUS, null));
        tts = new TextToSpeech(this, result -> {
            if (result != TextToSpeech.SUCCESS) {
                return;
            }
            int language = tts.setLanguage(new Locale("es", "MX"));
            if (language == TextToSpeech.LANG_MISSING_DATA || language == TextToSpeech.LANG_NOT_SUPPORTED) {
                tts.setLanguage(new Locale("es", "ES"));
            }
            tts.setSpeechRate(0.82f);
            ttsReady = true;
            if (startWhenReady) {
                startWhenReady = false;
                begin();
            }
        });
    }

    private void onTalk() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, MIC);
            return;
        }
        if (!isTalkBackActive() && !ttsReady) {
            startWhenReady = true;
            setStatus("Un momento");
            return;
        }
        begin();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == MIC && grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            onTalk();
        } else {
            setStatus("Hace falta el micrófono");
        }
    }

    private void begin() {
        setBusy(true);
        phase = "menu";
        speakThenListen(MENU);
    }

    private void sendTurn(String text) {
        setStatus(text);
        boolean searching = "confirm".equals(phase) && isYes(text);
        boolean[] spoken = {!searching};
        boolean[] ready = {false};
        boolean[] delivered = {false};
        JSONObject[] response = {null};
        String[] failure = {null};
        Runnable deliver = () -> {
            if (delivered[0] || !spoken[0] || (!ready[0] && failure[0] == null)) {
                return;
            }
            delivered[0] = true;
            stopWaitingSound();
            if (failure[0] != null) {
                speak(failure[0], this::setIdle);
            } else {
                onReply(response[0]);
            }
        };
        if (searching) {
            startWaitingSound();
            speak("Buscando videos", () -> {
                spoken[0] = true;
                deliver.run();
            });
        }
        network.execute(() -> {
            try {
                JSONObject body = new JSONObject();
                body.put("session", session);
                body.put("phase", phase);
                body.put("text", text);
                JSONObject result = post(body);
                runOnUiThread(() -> {
                    response[0] = result;
                    ready[0] = true;
                    deliver.run();
                });
            } catch (Exception error) {
                runOnUiThread(() -> {
                    failure[0] = "Hubo un problema. Pulsa el botón e inténtalo otra vez.";
                    deliver.run();
                });
            }
        });
    }

    private void onReply(JSONObject response) {
        String say = response.optString("say", "No entendí.");
        phase = response.optString("phase", "topic");
        setStatus(say);
        if ("choice".equals(phase) || "confirm".equals(phase) || "ask".equals(phase) || "menu".equals(phase)) {
            speakThenListen(say);
        } else {
            speak(say, this::setIdle);
        }
    }

    private void speakThenListen(String text) {
        speak(text + " Habla después del bip.", () -> listen(this::sendTurn));
    }

    private void listen(java.util.function.Consumer<String> onText) {
        main.postDelayed(() -> startListen(onText, 0), 250);
    }

    private void startListen(java.util.function.Consumer<String> onText, int attempt) {
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            speak("Este teléfono no puede escuchar.", this::setIdle);
            return;
        }
        if (recognizer == null) {
            recognizer = SpeechRecognizer.createSpeechRecognizer(this);
        } else {
            recognizer.cancel();
        }
        recognizer.setRecognitionListener(new RecognitionListener() {
            private boolean settled;
            private boolean ready;

            private void once(Runnable action) {
                if (settled) {
                    return;
                }
                settled = true;
                action.run();
            }

            @Override
            public void onReadyForSpeech(Bundle params) {
                ready = true;
                setStatus("Te escucho");
                playReadyBeep();
            }

            @Override
            public void onResults(Bundle results) {
                var list = results.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
                String heard = list == null || list.isEmpty() ? "" : list.get(0);
                if (heard.isEmpty() && ("choice".equals(phase) || "confirm".equals(phase))) {
                    once(this::again);
                    return;
                }
                once(() -> onText.accept(heard));
            }

            @Override
            public void onError(int error) {
                if (error == SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS) {
                    once(() -> speak("Hace falta el micrófono.", MainActivity.this::setIdle));
                    return;
                }
                if (!ready && attempt < 6) {
                    once(() -> main.postDelayed(() -> startListen(onText, attempt + 1), 1000));
                    return;
                }
                if ("choice".equals(phase) || "confirm".equals(phase)) {
                    once(this::again);
                    return;
                }
                once(() -> speak("No te escuché. Pulsa el botón e inténtalo otra vez.", MainActivity.this::setIdle));
            }

            private void again() {
                if ("menu".equals(phase)) {
                    speakThenListen(MENU);
                } else if ("confirm".equals(phase)) {
                    speakThenListen("No entendí. Di sí o no.");
                } else {
                    speakThenListen("No entendí. Di el número otra vez.");
                }
            }

            @Override public void onBeginningOfSpeech() {}
            @Override public void onRmsChanged(float rmsdB) {}
            @Override public void onBufferReceived(byte[] buffer) {}
            @Override public void onEndOfSpeech() {}
            @Override public void onPartialResults(Bundle partialResults) {}
            @Override public void onEvent(int eventType, Bundle params) {}
        });
        Intent intent = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
        intent.putExtra(RecognizerIntent.EXTRA_LANGUAGE, "es-MX");
        intent.putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1);
        boolean choosing = "choice".equals(phase) || "confirm".equals(phase) || "menu".equals(phase);
        intent.putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_MINIMUM_LENGTH_MILLIS, choosing ? 250L : 1500L);
        intent.putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_COMPLETE_SILENCE_LENGTH_MILLIS, choosing ? 1400L : 2500L);
        intent.putExtra(RecognizerIntent.EXTRA_SPEECH_INPUT_POSSIBLY_COMPLETE_SILENCE_LENGTH_MILLIS, choosing ? 900L : 1800L);
        setStatus("Te escucho");
        recognizer.startListening(intent);
    }

    private void speak(String text, Runnable after) {
        if (recognizer != null) {
            recognizer.cancel();
        }
        int generation = ++speakGeneration;
        if (isTalkBackActive()) {
            talk.announceForAccessibility(text);
            waitUntilTalkBackQuiet(generation, text.length(), after);
            return;
        }
        String utteranceId = UUID.randomUUID().toString();
        boolean[] finished = {false};
        Runnable finish = () -> {
            if (finished[0] || generation != speakGeneration) {
                return;
            }
            finished[0] = true;
            waitUntilQuiet(generation, after);
        };
        main.postDelayed(() -> {
            if (generation != speakGeneration || tts == null) {
                return;
            }
            tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                @Override
                public void onStart(String id) {}

                @Override
                public void onDone(String doneId) {
                    if (utteranceId.equals(doneId)) {
                        main.post(finish);
                    }
                }

                @Override
                public void onError(String doneId) {
                    if (utteranceId.equals(doneId)) {
                        main.post(finish);
                    }
                }
            });
            tts.speak(text, TextToSpeech.QUEUE_FLUSH, null, utteranceId);
            main.postDelayed(finish, Math.max(12000, text.length() * 140L));
        }, 300);
    }

    private void waitUntilQuiet(int generation, Runnable after) {
        Runnable check = new Runnable() {
            private int waits;

            @Override
            public void run() {
                if (generation != speakGeneration) {
                    return;
                }
                if (tts != null && tts.isSpeaking() && waits < 50) {
                    waits++;
                    main.postDelayed(this, 200);
                    return;
                }
                main.postDelayed(() -> {
                    if (generation == speakGeneration) {
                        after.run();
                    }
                }, 400);
            }
        };
        main.post(check);
    }

    private JSONObject post(JSONObject body) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(API).openConnection();
        connection.setConnectTimeout(10000);
        connection.setReadTimeout(70000);
        connection.setRequestMethod("POST");
        connection.setRequestProperty("Content-Type", "application/json");
        connection.setDoOutput(true);
        byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
        try (OutputStream output = connection.getOutputStream()) {
            output.write(bytes);
        }
        BufferedReader reader = new BufferedReader(new InputStreamReader(
                connection.getResponseCode() >= 400 ? connection.getErrorStream() : connection.getInputStream(),
                StandardCharsets.UTF_8));
        StringBuilder raw = new StringBuilder();
        String line;
        while ((line = reader.readLine()) != null) {
            raw.append(line);
        }
        reader.close();
        return new JSONObject(raw.toString());
    }

    private void setBusy(boolean busy) {
        this.busy = busy;
        talk.setBackgroundColor(busy ? 0xFFCC6600 : 0xFF00BB55);
    }

    private void setIdle() {
        setBusy(false);
    }

    private boolean isYes(String text) {
        String lowered = text == null ? "" : text.toLowerCase(Locale.ROOT);
        if (lowered.matches(".*\\bno\\b.*")) {
            return false;
        }
        return lowered.matches(".*\\b(sí|si|claro|bueno|ok|dale|bien|busca|buscar)\\b.*");
    }

    private void waitUntilTalkBackQuiet(int generation, int characters, Runnable after) {
        AudioManager audio = (AudioManager) getSystemService(AUDIO_SERVICE);
        long started = android.os.SystemClock.elapsedRealtime();
        long fallback = Math.max(1600, characters * 55L);
        Runnable check = new Runnable() {
            private boolean heard;

            @Override
            public void run() {
                if (generation != speakGeneration) {
                    return;
                }
                boolean playing = audio != null && !audio.getActivePlaybackConfigurations().isEmpty();
                if (playing) {
                    heard = true;
                }
                long elapsed = android.os.SystemClock.elapsedRealtime() - started;
                if (heard && !playing) {
                    main.postDelayed(() -> {
                        if (generation == speakGeneration) {
                            after.run();
                        }
                    }, 400);
                    return;
                }
                if ((!heard && elapsed >= fallback) || elapsed > fallback + 8000) {
                    if (generation == speakGeneration) {
                        after.run();
                    }
                    return;
                }
                main.postDelayed(this, 200);
            }
        };
        main.postDelayed(check, 250);
    }

    private void startWaitingSound() {
        stopWaitingSound();
        int stream = isTalkBackActive() ? AudioManager.STREAM_ACCESSIBILITY : AudioManager.STREAM_MUSIC;
        try {
            waitingTone = new ToneGenerator(stream, 100);
        } catch (RuntimeException ignored) {
            return;
        }
        waitingTick = new Runnable() {
            @Override
            public void run() {
                if (waitingTone == null) {
                    return;
                }
                waitingTone.startTone(ToneGenerator.TONE_PROP_BEEP, 180);
                main.postDelayed(this, 700);
            }
        };
        waitingTick.run();
    }

    private void stopWaitingSound() {
        if (waitingTick != null) {
            main.removeCallbacks(waitingTick);
            waitingTick = null;
        }
        if (waitingTone != null) {
            waitingTone.release();
            waitingTone = null;
        }
    }

    private void playReadyBeep() {
        int stream = isTalkBackActive() ? AudioManager.STREAM_ACCESSIBILITY : AudioManager.STREAM_MUSIC;
        try {
            ToneGenerator tone = new ToneGenerator(stream, 100);
            tone.startTone(ToneGenerator.TONE_PROP_BEEP, 150);
            main.postDelayed(tone::release, 400);
        } catch (RuntimeException ignored) {
        }
    }

    private boolean isTalkBackActive() {
        if (Settings.Secure.getInt(getContentResolver(), Settings.Secure.ACCESSIBILITY_ENABLED, 0) != 1) {
            return false;
        }
        String enabled = Settings.Secure.getString(
                getContentResolver(), Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
        return enabled != null && enabled.toLowerCase(Locale.ROOT).contains("talkback");
    }

    private void setStatus(String text) {
        status.setText(text);
    }

    @Override
    protected void onDestroy() {
        stopWaitingSound();
        main.removeCallbacksAndMessages(null);
        if (recognizer != null) {
            recognizer.destroy();
        }
        if (tts != null) {
            tts.shutdown();
        }
        network.shutdownNow();
        super.onDestroy();
    }
}
