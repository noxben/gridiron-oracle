// PasscodeEntry.jsx — Gridiron Oracle
// Simple 4-digit passcode entry screen.
// Commissioner shares each manager's code privately (text/DM).
// Valid code → team auto-detected → app loads with that team's data.

import { useState, useRef, useEffect } from 'react';
import { TEAM_CODES } from '../utils/TeamContext.jsx';
import { ALL_TEAMS } from '../utils/espn_league.js';

const C = {
  bg:      '#0a0c0f',
  surface: '#0f1318',
  border:  '#1e2328',
  text:    '#e8e6e0',
  textMid: '#5a6270',
  textDim: '#3a4048',
  accent:  '#c8ff00',
  red:     '#e06060',
};

const font  = '"DM Mono", "Fira Mono", "Consolas", monospace';
const serif = '"DM Serif Display", "Georgia", serif';

export default function PasscodeEntry({ onSuccess }) {
  const [digits,  setDigits]  = useState(['', '', '', '']);
  const [error,   setError]   = useState('');
  const [shake,   setShake]   = useState(false);
  const [success, setSuccess] = useState(false);
  const inputRefs = [useRef(), useRef(), useRef(), useRef()];

  // Focus first input on mount
  useEffect(() => {
    inputRefs[0].current?.focus();
  }, []);

  const handleDigit = (i, val) => {
    // Accept only digits
    const digit = val.replace(/\D/g, '').slice(-1);
    const next  = [...digits];
    next[i]     = digit;
    setDigits(next);
    setError('');

    // Auto-advance focus
    if (digit && i < 3) {
      inputRefs[i + 1].current?.focus();
    }

    // Auto-submit when all four digits filled
    if (digit && i === 3) {
      const code = [...next.slice(0, 3), digit].join('');
      if (code.length === 4) submit(code, next);
    }
  };

  const handleKeyDown = (i, e) => {
    if (e.key === 'Backspace' && !digits[i] && i > 0) {
      inputRefs[i - 1].current?.focus();
    }
    if (e.key === 'Enter') {
      const code = digits.join('');
      if (code.length === 4) submit(code, digits);
    }
  };

  // Handle paste of full code
  const handlePaste = (e) => {
    e.preventDefault();
    const pasted = e.clipboardData.getData('text').replace(/\D/g, '').slice(0, 4);
    if (pasted.length === 4) {
      setDigits(pasted.split(''));
      inputRefs[3].current?.focus();
      submit(pasted, pasted.split(''));
    }
  };

  const submit = (code, digitArr) => {
    const teamId = TEAM_CODES[code];
    if (!teamId) {
      setShake(true);
      setError('Invalid code. Check with your commissioner.');
      setTimeout(() => setShake(false), 600);
      setDigits(['', '', '', '']);
      setTimeout(() => inputRefs[0].current?.focus(), 50);
      return;
    }

    // Success
    const team = (ALL_TEAMS ?? []).find(t => t.team_id === teamId);
    setSuccess(true);
    setTimeout(() => onSuccess(teamId), 800);
  };

  const filledCount = digits.filter(Boolean).length;

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Serif+Display&display=swap');
        @keyframes shake {
          0%, 100% { transform: translateX(0); }
          20%       { transform: translateX(-8px); }
          40%       { transform: translateX(8px); }
          60%       { transform: translateX(-5px); }
          80%       { transform: translateX(5px); }
        }
        @keyframes fadeIn {
          from { opacity: 0; transform: translateY(6px); }
          to   { opacity: 1; transform: translateY(0); }
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: ${C.bg}; }
        input:focus { outline: none; }
      `}</style>

      <div style={{
        minHeight:      '100vh',
        background:     C.bg,
        color:          C.text,
        fontFamily:     font,
        display:        'flex',
        flexDirection:  'column',
      }}>
        {/* Header */}
        <header style={{
          borderBottom: `1px solid ${C.border}`,
          padding:      '24px 40px',
          display:      'flex',
          alignItems:   'baseline',
          gap:          '16px',
        }}>
          <span style={{ fontSize: '13px', fontWeight: '600', letterSpacing: '0.18em', textTransform: 'uppercase', color: C.accent }}>
            Gridiron Oracle
          </span>
          <span style={{ fontSize: '11px', letterSpacing: '0.12em', textTransform: 'uppercase', color: C.textDim }}>
            Fantasy Analytics Engine
          </span>
          <span style={{ marginLeft: 'auto', fontSize: '10px', color: C.textDim }}>
            League 839979 · #siscocks
          </span>
        </header>

        {/* Main */}
        <main style={{
          flex:           1,
          display:        'flex',
          alignItems:     'center',
          justifyContent: 'center',
          padding:        '40px',
        }}>
          <div style={{
            maxWidth:  '400px',
            width:     '100%',
            animation: 'fadeIn 0.3s ease',
          }}>
            <div style={{
              fontSize:      '10px',
              letterSpacing: '0.20em',
              textTransform: 'uppercase',
              color:         C.textDim,
              marginBottom:  '20px',
            }}>
              Manager access
            </div>

            <h1 style={{
              fontSize:      '32px',
              fontFamily:    serif,
              fontWeight:    '400',
              color:         C.text,
              lineHeight:    1.2,
              marginBottom:  '10px',
            }}>
              Enter your code
            </h1>
            <p style={{
              fontSize:     '13px',
              color:        C.textMid,
              lineHeight:   1.6,
              marginBottom: '40px',
            }}>
              4-digit code from your commissioner.
              Opens your team's view — no account needed.
            </p>

            {/* Digit inputs */}
            <div style={{
              display:       'flex',
              gap:           '12px',
              marginBottom:  '24px',
              animation:     shake ? 'shake 0.5s ease' : 'none',
            }}>
              {digits.map((d, i) => (
                <input
                  key={i}
                  ref={inputRefs[i]}
                  type="text"
                  inputMode="numeric"
                  maxLength={1}
                  value={d}
                  onChange={e => handleDigit(i, e.target.value)}
                  onKeyDown={e => handleKeyDown(i, e)}
                  onPaste={i === 0 ? handlePaste : undefined}
                  style={{
                    width:         '72px',
                    height:        '80px',
                    background:    C.surface,
                    border:        `1px solid ${d ? C.accent + '60' : C.border}`,
                    borderRadius:  '6px',
                    color:         success ? C.accent : C.text,
                    fontFamily:    font,
                    fontSize:      '28px',
                    fontWeight:    '500',
                    textAlign:     'center',
                    cursor:        'text',
                    transition:    'border-color 0.15s, color 0.15s',
                    caretColor:    'transparent',
                  }}
                />
              ))}
            </div>

            {/* Error */}
            {error && (
              <div style={{
                fontSize:     '12px',
                color:        C.red,
                marginBottom: '16px',
                padding:      '10px 14px',
                background:   C.red + '12',
                borderRadius: '4px',
                border:       `1px solid ${C.red}25`,
              }}>
                {error}
              </div>
            )}

            {/* Success feedback */}
            {success && (
              <div style={{
                fontSize:     '12px',
                color:        C.accent,
                marginBottom: '16px',
                padding:      '10px 14px',
                background:   C.accent + '12',
                borderRadius: '4px',
                animation:    'fadeIn 0.2s ease',
              }}>
                ✓ Code accepted — loading your team…
              </div>
            )}

            {/* Submit hint */}
            {!success && filledCount === 4 && !error && (
              <div style={{ fontSize: '11px', color: C.textDim }}>
                Press Enter to confirm
              </div>
            )}
            {!success && filledCount < 4 && (
              <div style={{ fontSize: '11px', color: C.textDim }}>
                {4 - filledCount} digit{4 - filledCount !== 1 ? 's' : ''} remaining
              </div>
            )}

            <div style={{ marginTop: '48px', fontSize: '11px', color: C.textDim, lineHeight: 1.8 }}>
              Don't have a code?{' '}
              <span style={{ color: C.textMid }}>Ask your commissioner.</span>
            </div>
          </div>
        </main>
      </div>
    </>
  );
}
