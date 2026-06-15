// Email verification handler — polls Gmail API for verify links

function extractVerifyLinks(body) {
  const links = [];
  const re = /https?:\/\/[^\s"'<>]+(?:verif|confirm|activat|validate)[^\s"'<>]*/gi;
  let m;
  while ((m = re.exec(body)) !== null) links.push(m[0]);
  return links;
}

function decodeGmailBody(payload) {
  // Decode base64url Gmail body
  function b64decode(str) {
    try {
      return atob(str.replace(/-/g, '+').replace(/_/g, '/'));
    } catch(e) { return ''; }
  }

  if (payload.body && payload.body.data) {
    return b64decode(payload.body.data);
  }
  if (payload.parts) {
    for (const part of payload.parts) {
      const text = decodeGmailBody(part);
      if (text) return text;
    }
  }
  return '';
}

async function checkEmailVerification(fromDomain, timeoutMs = 90000) {
  const start = Date.now();

  while (Date.now() - start < timeoutMs) {
    try {
      const thread = await chrome.runtime.sendMessage({
        type: 'GMAIL_SEARCH',
        query: `from:${fromDomain} newer_than:5m (verify OR confirm OR activate OR validate)`,
      });

      if (thread && thread.payload) {
        const body = decodeGmailBody(thread.payload);
        const links = extractVerifyLinks(body);
        if (links.length > 0) {
          // Open verify link in a new tab then close it
          await chrome.runtime.sendMessage({
            type: 'OPEN_VERIFY_LINK',
            link: links[0],
          });
          return true;
        }
      }
    } catch(e) {
      console.debug('JobberAI: email_verify poll error:', e);
    }

    await new Promise(r => setTimeout(r, 3000));
  }

  // Fallback: notify user to verify manually
  console.warn('JobberAI: email verification timed out for', fromDomain);
  return false;
}

async function checkForOTP(fromDomain) {
  try {
    const thread = await chrome.runtime.sendMessage({
      type: 'GMAIL_SEARCH',
      query: `from:${fromDomain} newer_than:5m (OTP OR "one-time" OR "verification code" OR "your code")`,
    });
    if (!thread || !thread.payload) return null;
    const body = decodeGmailBody(thread.payload);
    // Extract 4-8 digit codes
    const codeMatch = body.match(/\b(\d{4,8})\b/);
    return codeMatch ? codeMatch[1] : null;
  } catch(e) { return null; }
}
