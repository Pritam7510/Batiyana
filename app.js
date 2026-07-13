(function() {
  const startBtn = document.getElementById('voice-start');
  const transcriptEl = document.getElementById('voice-transcript');
  const statusEl = document.getElementById('voice-status');
  const voiceResponseEl = document.getElementById('voice-response');
  const cmdTypeEl = document.getElementById('command-type');
  const intentCard = document.getElementById('voice-intent-card');
  const confirmationModal = document.getElementById('confirmationModal');
  const confirmTitleEl = document.getElementById('confirmTitle');
  const confirmMessageEl = document.getElementById('confirmMessage');
  const commandHistoryContainer = document.getElementById('command-history-container');
  const pulse = document.getElementById('voice-pulse');

  if (!startBtn || !transcriptEl || !statusEl || !intentCard || !commandHistoryContainer) {
    return;
  }

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const synth = window.speechSynthesis;
  let recognition = null;
  let currentTranscript = '';

  if (!SpeechRecognition) {
    statusEl.textContent = 'Speech recognition is not supported in this browser.';
    startBtn.disabled = true;
    return;
  }
  let listening = false;
  let pendingConfirmation = null;
  let submitTimer = null;

  function speak(text) {
    if (!synth) return;
    synth.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1.0;
    utterance.pitch = 1.0;
    speak.velocity = 1.0;
    synth.speak(utterance);
  }

  function updateUi() {
    if (listening) {
      startBtn.textContent = '🎤 Stop Listening';
      statusEl.textContent = 'Listening… speak clearly.';
      pulse.classList.add('listening');
    } else {
      startBtn.textContent = '🎤 Start Listening';
      pulse.classList.remove('listening');
    }
  }

  function setTranscript(text) {
    currentTranscript = text.trim();
    transcriptEl.textContent = currentTranscript ? `“${currentTranscript}”` : 'Try saying something here...';
  }

  function showIntentCard(commandType, response) {
    intentCard.style.display = 'block';
    voiceResponseEl.textContent = response;
    cmdTypeEl.textContent = commandType || 'Action';
  }

  function hideIntentCard() {
    intentCard.style.display = 'none';
  }

  function showConfirmation(title, message, data) {
    confirmTitleEl.textContent = title;
    confirmMessageEl.textContent = message;
    pendingConfirmation = data;
    confirmationModal.style.display = 'flex';
    speak(message);
  }

  function clearHistory(e) {
    e.preventDefault();
    fetch('/voice/history/clear', { method: 'POST' })
      .finally(() => {
        commandHistoryContainer.innerHTML = '';
      });
  }

  function addToHistory(text, status) {
    const entry = document.createElement('div');
    entry.className = 'row voice-command-log';
    entry.innerHTML = `
      <div class="row-icon" style="background:#DCE7FD;color:var(--sky);">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>
      </div>
      <div class="row-body">
        <div class="row-title">“${text}”</div>
        <div class="row-sub">Just now • ${status}</div>
      </div>
    `;
    commandHistoryContainer.insertBefore(entry, commandHistoryContainer.firstChild);
    while (commandHistoryContainer.children.length > 8) {
      commandHistoryContainer.removeChild(commandHistoryContainer.lastChild);
    }
  }

  function submitVoiceCommand(transcript, confirmed = false) {
    if (!transcript) return;
    statusEl.textContent = 'Processing your command...';
    hideIntentCard();

    fetch('/voice/submit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ transcript: transcript, confirmed: confirmed })
    })
      .then((response) => {
        const contentType = response.headers.get('content-type') || '';
        if (!response.ok || !contentType.includes('application/json')) {
          throw new Error('session_or_server_error');
        }
        return response.json();
      })
      .then((data) => {
        if (data.needs_confirmation) {
          addToHistory(transcript, 'Needs Confirmation');
          showConfirmation('Confirm action', data.message, { transcript });
          return;
        }

        if (data.ok) {
          showIntentCard(data.command_type, data.message);
          addToHistory(transcript, 'Completed');
          speak(data.message);
          statusEl.textContent = data.message;
          setTranscript('');

          if (data.action_result && data.action_result.navigate) {
            setTimeout(() => { window.location.href = data.action_result.navigate; }, 1000);
            return;
          }

          if (data.action_result && data.action_result.action === 'logout') {
            setTimeout(() => { window.location.href = '/logout'; }, 1000);
            return;
          }

          if (data.action_result && data.action_result.action === 'refresh') {
            setTimeout(() => { window.location.reload(); }, 1000);
            return;
          }

          setTimeout(() => { window.location.reload(); }, 1300);
        } else {
          showIntentCard(data.command_type || 'Error', data.message);
          addToHistory(transcript, 'Failed');
          speak(data.message);
          statusEl.textContent = data.message;
        }
      })
      .catch((error) => {
        const errorMsg = error.message === 'session_or_server_error'
          ? 'Session expired or server did not respond correctly. Please log in again.'
          : 'Unable to reach the server. Please try again.';
        showIntentCard('Error', errorMsg);
        addToHistory(transcript, 'Error');
        speak(errorMsg);
        statusEl.textContent = errorMsg;
      });
  }

  function startRecognition() {
    if (!recognition) return;
    setTranscript('');
    statusEl.textContent = 'Listening… speak now.';
    hideIntentCard();
    pendingConfirmation = null;
    confirmationModal.style.display = 'none';
    recognition.start();
  }

  recognition = new SpeechRecognition();
  recognition.interimResults = true;
  recognition.lang = 'en-US';
  recognition.continuous = false;

  recognition.onstart = function() {
    listening = true;
    updateUi();
  };

  recognition.onresult = function(event) {
    clearTimeout(submitTimer);
    let transcript = '';
    let isFinal = false;
    for (let i = 0; i < event.results.length; i += 1) {
      transcript += event.results[i][0].transcript;
      if (event.results[i].isFinal) {
        isFinal = true;
      }
    }
    setTranscript(transcript);
    statusEl.textContent = currentTranscript ? 'Reviewing your command...' : 'Listening…';
    if (isFinal) {
      submitTimer = setTimeout(() => submitVoiceCommand(currentTranscript, false), 700);
    }
  };

  recognition.onend = function() {
    listening = false;
    updateUi();
  };

  recognition.onerror = function(event) {
    listening = false;
    const message = {
      'no-speech': 'No speech detected. Please try again.',
      'audio-capture': 'No microphone found. Please check your device.',
      'not-allowed': 'Microphone access denied. Allow permissions and try again.',
      'network': 'Network error. Verify your connection.'
    }[event.error] || `Speech error: ${event.error}`;
    statusEl.textContent = message;
    updateUi();
  };

  startBtn.addEventListener('click', function() {
    if (listening) {
      recognition.stop();
      return;
    }
    startRecognition();
  });

  window.confirmCommand = function() {
    if (!pendingConfirmation) return;
    confirmationModal.style.display = 'none';
    submitVoiceCommand(pendingConfirmation.transcript, true);
  };

  window.closeConfirmation = function() {
    pendingConfirmation = null;
    confirmationModal.style.display = 'none';
  };

  window.clearHistory = clearHistory;

  function resetVoiceState() {
    listening = false;
    setTranscript('');
    statusEl.textContent = 'Tap the button to start speaking';
    pulse.classList.remove('listening');
    hideIntentCard();
  }

  resetVoiceState();
})();
