// Autofill engine — ported from fast_autofill.py + screener_engine.py
// Three-tier resolution: alias → regex bank → memory → Claude

const LABEL_ALIASES = {
  "full name":                "full_name",
  "name":                     "full_name",
  "your name":                "full_name",
  "first name":               "first_name",
  "firstname":                "first_name",
  "given name":               "first_name",
  "last name":                "last_name",
  "lastname":                 "last_name",
  "family name":              "last_name",
  "surname":                  "last_name",
  "e-mail":                   "email",
  "email address":            "email",
  "your email":               "email",
  "email id":                 "email",
  "enter your email":         "email",
  "enter email":              "email",
  "mobile":                   "phone",
  "mobile number":            "phone",
  "telephone":                "phone",
  "cell":                     "phone",
  "cell phone":               "phone",
  "phone number":             "phone",
  "phone":                    "phone",
  "contact number":           "phone",
  "phone country code":       "phone_country_code",
  "country code":             "phone_country_code",
  "linkedin profile":         "linkedin_url",
  "linkedin profile url":     "linkedin_url",
  "linkedin url":             "linkedin_url",
  "linkedin":                 "linkedin_url",
  "github profile":           "github_url",
  "github url":               "github_url",
  "github":                   "github_url",
  "website":                  "portfolio_url",
  "personal website":         "portfolio_url",
  "portfolio":                "portfolio_url",
  "portfolio url":            "portfolio_url",
  "city":                     "address_city",
  "state":                    "address_state",
  "state/province":           "address_state",
  "zip":                      "address_zip",
  "zip code":                 "address_zip",
  "postal code":              "address_zip",
  "postcode":                 "address_zip",
  "country":                  "address_country",
  "street address":           "address_line1",
  "address":                  "address_line1",
  "address line 1":           "address_line1",
  "address 1":                "address_line1",
  "phone extension":          null,
  "legal name":               "full_name",
  "legal full name":          "full_name",
  "preferred name":           "first_name",
  "preferred first name":     "first_name",
  "desired salary":           "salary_expectation",
  "expected salary":          "salary_expectation",
  "salary expectation":       "salary_expectation",
  "compensation":             "salary_expectation",
  "salary":                   "salary_expectation",
  "years of experience":      "years_experience",
  "years experience":         "years_experience",
  "how many years":           "years_experience",
  "years of relevant":        "years_experience",
  "years of professional":    "years_experience",
  "total years":              "years_experience",
  "current ctc":              "salary_expectation",
  "expected ctc":             "salary_expectation",
  "notice period":            null,
  "work authorization":       "work_authorization",
  "authorized to work":       "work_authorization",
  "graduation year":          "graduation_year",
  "degree":                   "education_degree",
  "highest degree":           "education_degree",
  "highest education":        "education_degree",
  "cover letter":             "cover_letter_default",
  "message":                  "cover_letter_default",
  "why are you interested":   "cover_letter_default",
  "why this company":         "cover_letter_default",
  "why this role":            "cover_letter_default",
  "where are you currently located":  "location",
  "where are you located":            "location",
  "current location":                 "location",
  "your location":                    "location",
  "city, state":                      "location",
  "city or state":                    "location",
  "what is your location":            "address_country",
  "location (country)":               "address_country",
  "country of residence":             "address_country",
  "country of location":              "address_country",
  "what is your preferred name":      "first_name",
  "preferred name":                   "first_name",
  "preferred first name":             "first_name",
  "skills":                   "skills",
  "technical skills":         "skills",
  "key skills":               "skills",
  "technologies":             "skills",
  "tools":                    "skills",
  "tools & technologies":     "skills",
  "programming languages":    "skills_by_category.languages",
  "languages":                "spoken_languages",
  "frameworks":               "skills_by_category.ml_frameworks",
  "libraries":                "skills_by_category.ml_frameworks",
  "ml frameworks":            "skills_by_category.ml_frameworks",
  "cloud platforms":          "skills_by_category.cloud_devops",
  "cloud":                    "skills_by_category.cloud_devops",
  "devops tools":             "skills_by_category.cloud_devops",
  "current company":          "current_company",
  "current employer":         "current_company",
  "employer":                 "current_company",
  "company":                  "current_company",
  "organization":             "current_company",
  "bio":                      "bio",
  "about you":                "bio",
  "about yourself":           "bio",
  "professional summary":     "bio",
  "summary":                  "bio",
  "headline":                 "bio",
  "notice period":            "notice_period",
  "available start date":     "start_date",
  "start date":               "start_date",
  "previously worked here":   "previously_worked_here",
  "have you worked here":     "previously_worked_here",
  "have you previously worked here": "previously_worked_here",
  "other website":            "portfolio_url",
  "other websites":           "portfolio_url",
  "other url":                "portfolio_url",
  "other link":               "portfolio_url",
  "additional links":         "portfolio_url",
  "strength":                 "strength",
  "greatest strength":        "strength",
  "address line 2":           null,
  "apartment":                null,
  "suite":                    null,
  // Education sub-fields
  "discipline":               "education_field",
  "major":                    "education_field",
  "field of study":           "education_field",
  "area of study":            "education_field",
  "concentration":            "education_field",
  // Spoken languages (not programming languages)
  "languages spoken":         "spoken_languages",
  "spoken languages":         "spoken_languages",

  // Workday-specific
  "legal first name":         "first_name",
  "legal last name":          "last_name",
  "legal name - first":       "first_name",
  "legal name - last":        "last_name",
  "legal middle name":        null,
  "preferred first name":     "first_name",
  "country phone code":       "phone_country_code",
  "phone device type":        null,
  "how did you hear about us": "referral_source",
  "how did you hear about this position": "referral_source",
  "how did you hear about this job": "referral_source",
  "pronoun":                  "pronouns",
  "gender identity":          "eeo_gender",

  // LinkedIn Easy Apply / generic
  "mobile phone number":      "phone",
  "city state zip code":      "location",
  "your city state or country": "location",
  "what is the city you live in": "address_city",
  "current city":             "address_city",
  "permanent address city":   "address_city",
  "permanent address state":  "address_state",
  "your linkedin profile url": "linkedin_url",
  "linkedin profile url":     "linkedin_url",
  "your github profile url":  "github_url",
  "your portfolio or github":  "github_url",

  // iCIMS / SmartRecruiters
  "applicant name":           "full_name",
  "current job title":        "current_title",
  "most recent job title":    "current_title",
  "most recent employer":     "current_company",
  "recent employer":          "current_company",
  "most recent company":      "current_company",
  "current employer name":    "current_company",
  "name of most recent employer": "current_company",
  "most recent salary":       "salary_expectation",
  "current salary":           "salary_expectation",
  "desired compensation":     "salary_expectation",
  "minimum salary":           "salary_expectation",
  "preferred salary":         "salary_expectation",

  // Greenhouse/Lever variants
  "availability":             "_start_date",
  "available to start":       "_start_date",
  "earliest start date":      "_start_date",
  "job title":                "current_title",
  "position title":           "current_title",
  "employment type":          null,

  // ADP / Taleo
  "applicant email":          "email",
  "applicant phone":          "phone",
  "home phone":               "phone",
  "work phone":               "phone",
  "daytime phone":            "phone",
  "alternate phone":          null,
  "emergency contact":        null,
  "emergency contact phone":  null,
  "middle name":              null,
  "middle initial":           null,
  "suffix":                   null,
  "prefix":                   null,
  "title":                    null,
  "salutation":               null,
};

// Profile key lookup map (maps address_* and alias keys to profile flat keys)
const PROFILE_KEY_MAP = {
  "address_city":    "city",
  "address_state":   "state",
  "address_zip":     "zip_code",
  "address_country": "country",
  "address_line1":   "address",
  "location":        "location",
  "referral_source": "referral_source",
  "current_title":   "current_title",
  "_start_date":     "_start_date",  // resolved via STATIC_SYNTHETIC
};

const QUESTION_BANK = [
  // "Referred by a [company] employee" — section heading or question. Must be BEFORE work_authorization
  // to avoid "referred by a current lendbuzz employee" matching work.* or eligible.* patterns.
  { re: /referred.{0,30}(current|existing).{0,20}employee|current.{0,20}employee.{0,20}refer|employee.{0,20}referral.{0,20}(section|program)?$/i, key: "_no" },
  { re: /authorized.{0,30}work|right.{0,10}work|eligible.{0,15}work|legally.{0,15}work|lawfully|work.{0,15}authoriz|work.{0,15}eligib|work.{0,15}(status|permit)|immigration.{0,20}(status|authoriz)/i, key: "work_authorization" },
  { re: /sponsor|visa.{0,20}sponsor|require.{0,10}sponsor|need.{0,10}sponsor/i,                     key: "requires_sponsorship" },
  { re: /us.{0,5}citizen|permanent.{0,5}resid|green.{0,5}card/i,                                    key: "us_citizen_or_pr" },
  // Compensation acknowledgment questions — must come BEFORE salary|compensation or they match wrong key
  { re: /acknowledge.{0,50}(compensation|pay.{0,10}range|terms)|agree.{0,30}(compensation.{0,10}terms|pay.{0,10}range)|confirm.{0,30}(pay|compens).{0,20}(term|range|acknowledg)/i, key: "_yes" },
  { re: /salary|compensation|expected.{0,15}pay|desired.{0,10}pay|pay.{0,15}expect/i,               key: "salary_expectation" },
  { re: /years?.{0,10}experience|how.{0,15}years|experience.{0,10}years/i,                          key: "years_experience" },
  { re: /start.{0,10}date|available.{0,15}start|when.{0,15}start|earliest.{0,10}start/i,            key: "_start_date" },
  { re: /background.{0,10}check|consent.{0,20}background|drug.{0,10}test/i,                         key: "_yes" },
  { re: /remote|work.{0,15}remote|preference.{0,15}remote|remote.{0,15}work/i,                      key: "remote_preference" },
  { re: /reloc|willing.{0,15}reloc|open.{0,10}reloc|relocate/i,                                     key: "willing_to_relocate" },
  { re: /highest.{0,20}educ|education.{0,15}level|highest.{0,10}degree/i,                           key: "education_degree" },
  { re: /grad.{0,10}year|when.{0,15}graduat|expected.{0,15}grad|graduation/i,                       key: "graduation_year" },
  { re: /driver.{0,10}licen|driving.{0,10}licen|valid.{0,10}licen/i,                                key: "_yes" },
  { re: /18.{0,10}(years|or older)|over.{0,5}18|legal.{0,10}age|at least 18|18\+|over the age of 18/i, key: "_yes" },

  // Pronouns — group label OR individual checkbox option labels
  { re: /pronoun/i,                                                                                   key: "_pronouns" },
  // Individual pronoun checkbox options — check He/Him, skip all others
  { re: /^he\s*[\/|]\s*him$/i,                                                                        key: "_yes" },
  { re: /^she\s*[\/|]\s*her$/i,                                                                       key: "_no" },
  { re: /^they\s*[\/|]\s*them$/i,                                                                     key: "_no" },
  { re: /^xe\s*[\/|]\s*xem$/i,                                                                        key: "_no" },
  { re: /^ze\s*[\/|]\s*hir$/i,                                                                        key: "_no" },
  { re: /^ey\s*[\/|]\s*em$/i,                                                                         key: "_no" },
  { re: /^hir\s*[\/|]\s*hir$/i,                                                                       key: "_no" },
  { re: /^fae\s*[\/|]\s*faer$/i,                                                                      key: "_no" },
  { re: /^hu\s*[\/|]\s*hu$/i,                                                                         key: "_no" },
  { re: /^use\s+name\s+only$/i,                                                                        key: "_no" },
  { re: /^custom$/i,                                                                                    key: "_no" },
  { re: /^prefer not to say$|^no pronoun|^decline.*pronoun/i,                                         key: "_no" },

  // Today's date
  { re: /today.{0,10}date|current.{0,10}date|date.{0,10}today|date.{0,5}sign/i,                   key: "_today" },
  { re: /^date$|application.{0,10}date|submission.{0,10}date/i,                                    key: "_today" },

  // Common yes/no compliance questions
  { re: /relativ|family.{0,15}member|friend.{0,15}(work|employ)|know.{0,15}anyone.{0,15}(work|employ)/i, key: "_no" },
  { re: /spouse|domestic.{0,10}partner|significant.{0,10}other|partner.{0,10}(work|employ)/i,     key: "_no" },
  { re: /armed.{0,10}force|military.{0,10}(member|service|family)|active.{0,10}duty|serving.{0,10}military/i, key: "_no" },
  { re: /family.{0,15}(member|relative).{0,20}(armed|military|service)/i,                         key: "_no" },
  // Conditional follow-up text fields ("If yes, please provide…") — we always answer No to
  // referral/contact questions, so these follow-ups should always be N/A.
  { re: /^if yes[,\s]|^if so[,\s]|^if you answered yes|^if your answer is yes|^if applicable[,\s]/i, key: "_na" },
  // "If referred, please list the name…" — must be before the who-referred→_no pattern
  { re: /if.{0,10}referred.{0,40}(list|name|provide|who|person)|list.{0,20}name.{0,20}(referr|who referred)/i, key: "_na" },
  { re: /provide.{0,30}name.{0,30}(employ|referr|staff|colleague)|name.{0,20}(employ|referr).{0,20}(refer|who)|referr.{0,20}employee.{0,20}name/i, key: "_na" },
  // "Referral Details" section heading detected as field label — the text input below it wants N/A
  { re: /^referral\s*(detail|info|section)?s?$/i,                                                    key: "_na" },
  { re: /refer.{0,15}(by|from)|referred.{0,10}by|who referred/i,                                   key: "_no" },
  { re: /non.?compete|non.?disclosure|nda.{0,20}(current|previous|prior)/i,                        key: "_no" },
  { re: /felony|criminal.{0,20}(record|conviction|charge)|convicted|been arrested/i,                key: "_no" },
  { re: /terminat.{0,20}(for cause|involuntary)|fired.{0,15}for.{0,15}cause/i,                    key: "_no" },
  { re: /conflict.{0,15}interest/i,                                                                  key: "_no" },
  { re: /currently.{0,15}(active|hold).{0,15}security.{0,10}clearance/i,                           key: "_no" },
  { re: /security.{0,10}clearance/i,                                                                 key: "_no" },
  // "Previously worked at [company]?" — broad pattern covers "here", named company, etc.
  { re: /previously.{0,30}(work|employ).{0,40}(at|for|by|with|here|this company|with us|by us)|have you.{0,30}(work|been employ).{0,30}(at|for|by)|worked.{0,20}(here|for us|this company) before|ever.{0,20}work.{0,10}(at|for|here)/i, key: "previously_worked_here" },
  { re: /formerly.{0,20}employed|ever.{0,20}employed.{0,20}by/i,                                   key: "previously_worked_here" },
  { re: /contract.{0,10}(prevent|restrict|prohibit)|restrictive.{0,10}covenant/i,                  key: "_no" },
  { re: /currently.{0,15}employ|are you.{0,15}(currently.{0,5})?employ/i,                         key: "_yes" },
  { re: /on.?call|available.{0,10}on.?call/i,                                                       key: "_yes" },
  // Employment-type checkboxes (e.g. Lever's 'Yes, full-time employment' / 'Yes, intern')
  { re: /full.?time.{0,25}(employ|position|role|opportunit)/i,                                        key: "_yes" },
  { re: /^intern(\.?ship)?$|yes.{0,10}intern|intern.{0,20}(employ|opportunit|role)/i,                key: "_no"  },
  { re: /overtime|willing.{0,10}(to work )?overtime/i,                                              key: "_yes" },
  { re: /willing.{0,15}travel|open.{0,10}travel|travel.{0,10}required/i,                           key: "_yes" },
  { re: /drug.{0,10}test|substance.{0,10}test|consent.{0,10}drug/i,                               key: "_yes" },
  { re: /background.{0,10}check|consent.{0,20}background/i,                                        key: "_yes" },
  { re: /agree.{0,20}terms|accept.{0,20}terms|terms.{0,10}(and.{0,5})?condition/i,                key: "_yes" },
  { re: /essential.{0,20}function|perform.{0,20}(essential|job).{0,20}(function|dut)|with.{0,20}reasonable.{0,20}accommodation/i, key: "_yes" },
  { re: /meet.{0,20}(preferred|minimum|required).{0,20}qualif|qualif.{0,20}(for this|meet)/i,    key: "_yes" },
  { re: /minimum.{0,20}qualif|basic.{0,20}qualif|required.{0,20}qualif/i,                        key: "_yes" },
  { re: /how did you (hear|find|learn).{0,20}(about|of)|source of (application|referral)|where did you hear/i, key: "_heard" },
  // Office attendance / hybrid questions
  { re: /work.{0,20}(from.{0,10}office|in.{0,10}office|on.?site)|office.{0,15}(days|hours|week)|days?.{0,10}(per|a).{0,5}week.{0,20}(office|in.person|on.site)/i, key: "_yes" },
  { re: /commut|onsite|in.person.{0,20}(require|expect|able)/i,                                    key: "_yes" },
  { re: /comfortable.{0,40}(hybrid|in.?office|office.{0,10}require|work.{0,15}arrangement)|hybrid.{0,20}(work|arrangement|schedule|flexib)|in.?office.{0,20}(require|expect|able|meet|attend)/i, key: "_yes" },
  // Acknowledgment / legal checkboxes
  { re: /arbitration|i acknowledge|i confirm.{0,15}(read|above)|certify.{0,20}that|i have read.{0,20}(and|above)/i, key: "_yes" },
  { re: /agree.{0,20}(policy|above|statement)|accept.{0,20}(agreement|policy)/i,                   key: "_yes" },
  // Additional information / motivation → Claude
  { re: /additional.{0,20}information|motivation.{0,15}apply|additional.{0,15}context|anything.{0,20}(else|know)|is there.{0,20}anything/i, key: "_claude" },
  { re: /^phone$|phone.{0,10}number|mobile.{0,10}number|telephone/i,                                key: "phone" },
  { re: /linkedin|linkedin.{0,10}url|linkedin.{0,10}profile/i,                                      key: "linkedin_url" },
  { re: /github|github.{0,10}url|github.{0,10}profile/i,                                            key: "github_url" },
  { re: /portfolio|personal.{0,10}website|website.{0,10}url/i,                                      key: "portfolio_url" },
  { re: /cover.{0,10}letter|additional.{0,15}comment|anything.{0,20}add/i,                          key: "cover_letter_default" },
  // "Current location: city" / "location (city)" — must be BEFORE generic location→country
  { re: /location.{0,10}:?.{0,5}city|current.{0,15}location.{0,20}city|city.{0,10}(field|location|where)/i, key: "address_city" },
  // Demographic survey / location selects — country dropdown uses address_country, not state
  { re: /what.{0,10}(is|is your).{0,10}location|demographic.{0,15}location|survey.{0,10}location/i, key: "address_country" },
  // LGBTQ+ identification
  { re: /\blgbtq\b|lgbtq\+|sexual.{0,20}orientat.{0,20}(identif|self)|identify.{0,20}lgbtq/i,      key: "_no" },
  // Country questions (born-in vs currently-live must come before generic "country" alias)
  { re: /born.{0,20}(in|country)|country.{0,20}(were you born|of birth|born in)|what country.{0,20}(born|birth)/i, key: "birth_country" },
  { re: /currently live|country.{0,20}(do you live|reside|residing|currently|you live in)|live.{0,15}(in what country|country)/i, key: "address_country" },
  // Years lived in current country (options like 1 2 3 … +10)
  { re: /how many years.{0,30}(lived|living|reside|resided).{0,30}(current|this|your).{0,10}country|years.{0,20}(in|lived in).{0,20}(current|this|your).{0,10}country|years.{0,20}(of residence|in the us|in america|in united states)/i, key: "years_in_current_country" },
  // U.S. State (disambiguate from generic "state" alias which also maps to address_state)
  { re: /which.{0,15}(u\.?s\.?|united states).{0,15}state|state.{0,20}(based|located|reside|currently|you.{0,5}are)/i, key: "address_state" },
  // English language proficiency
  { re: /native.{0,25}(english|proficiency|speaker)|english.{0,25}(native|proficien|fluent)|proficien.{0,20}(u\.?s\.?.{0,5}english|english)/i, key: "_yes" },
  // Full-time availability (hours-per-week phrasing — not covered by employment-type pattern above)
  { re: /available.{0,20}(to work|for).{0,10}full.?time|full.?time.{0,20}(40 hours|hours.{0,10}week|\d+.hours)/i, key: "_yes" },
  // Consent / marketing checkboxes
  { re: /consent.{0,20}(contact|reach|email|newsletter)|may.{0,15}contact.{0,15}(me|you)|future.{0,20}(job|opportun|position)/i, key: "_yes" },
  { re: /\bgender\b|gender.{0,10}identity|describe.{0,20}gender/i,                                  key: "_gender" },
  { re: /\brace\b|\bethnicity\b|ethnic.{0,15}group|racial|race.{0,10}ethnic/i,                     key: "eeo_ethnicity" },
  { re: /veteran|military.{0,15}(status|service|served)|served.{0,20}military/i,                   key: "_no_veteran" },
  { re: /disabilit/i,                                                                                 key: "_no_disability" },
  // Currently employed → Yes (we have a current role at Hexbandit.io)
  // Note: "currently employed?" appears above; this covers variants without "are you"
  { re: /currently (working|at a company)|present.{0,10}employer|current.{0,5}role/i,               key: "_yes" },
  // Notice period / availability
  { re: /notice.{0,15}period|how soon.{0,20}(available|start)|how quickly.{0,15}start/i,            key: "_start_date" },
  // Comfortable working in new city / relocation city question
  { re: /comfortable.{0,30}(commut|on.?site|travel.{0,10}to (office|work))|willing.{0,20}commut/i,  key: "_yes" },
  // Do you have X years of experience? (generic proficiency Qs)
  { re: /do you have.{0,30}(experience|skill|knowledge|background|proficien)/i,                      key: "_yes" },
  // At least N years of experience?
  { re: /at least.{0,20}(year|yr).{0,10}(of )?(experience|exp)/i,                                   key: "_yes" },
  // Authorized to use personal vehicle / own car
  { re: /valid.{0,10}driver|personal.{0,10}vehicle|own.{0,5}car|use.{0,10}(own|personal).{0,10}(car|vehicle)/i, key: "_yes" },
  // Willing to work weekends / flexible schedule
  { re: /work.{0,15}weekend|flexible.{0,15}(schedule|hours)|varying.{0,10}(hour|shift)/i,           key: "_yes" },
  // "How many total years" / "years of professional experience" (number field)
  { re: /how many.{0,20}(total.{0,10})?years.{0,30}(professional|industry|relevant|work)/i,         key: "years_experience" },
  // City / location questions
  { re: /what.{0,10}city|city.{0,10}(you live|you're in|do you|based|located)/i,                   key: "address_city" },
  { re: /what.{0,10}state|state.{0,10}(you live|you're in|do you|based|located)/i,                 key: "address_state" },
  // Job title / most recent
  { re: /most recent.{0,10}(job.?)?title|current.{0,10}job.?title|your.{0,10}(current|present).{0,10}title/i, key: "current_title" },
  { re: /most recent.{0,15}(company|employer)|current.{0,10}company/i,                              key: "current_company" },
  // Desired / minimum / expected salary variants
  { re: /minimum.{0,20}salary|minimum.{0,10}pay|lowest.{0,10}(salary|compensation)/i,               key: "salary_expectation" },
  { re: /maximum.{0,20}salary|maximum.{0,10}pay/i,                                                   key: "salary_expectation" },
  // LinkedIn-specific EAL questions
  { re: /proficien.{0,30}(python|java|c\+\+|javascript|typescript|sql|aws|docker|kubernetes|git|react)/i, key: "_yes" },
  // Work type preference
  { re: /job type|type of (work|employment|position)|employment.{0,10}type/i,                        key: "_full_time" },
  // I agree / I certify / I acknowledge (broader)
  { re: /i (certify|attest|represent).{0,40}(true|correct|accurate|complete)/i,                      key: "_yes" },
  { re: /by submitting|by clicking.{0,20}submit|by applying/i,                                       key: "_yes" },
  // Diversity / belonging
  { re: /veteran.{0,20}status|military.{0,15}branch|branch.{0,15}service/i,                         key: "_no_veteran" },
  // Open-ended → Claude
  { re: /why.{0,20}(us|company|organization|firm)|interest.{0,20}(us|company)/i,                    key: "_claude" },
  { re: /why.{0,20}(role|position|job|this.{0,10}opport)/i,                                         key: "_claude" },
  { re: /strength|what.{0,20}bring|superpower|best.{0,10}qualit/i,                                  key: "_claude" },
  { re: /biggest.{0,20}project|notable.{0,20}project|proud.{0,20}project/i,                         key: "_claude" },
  { re: /challenge|difficult.{0,20}(situation|problem)|obstacle|overcome/i,                          key: "_claude" },
  { re: /tell.{0,20}(us|me).{0,20}(yourself|background)|introduce|brief.{0,10}bio/i,               key: "_claude" },
  { re: /relevant.{0,20}experience|describe.{0,20}experience/i,                                      key: "_claude" },
];

function _todayFormatted() {
  const d = new Date();
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  const yyyy = d.getFullYear();
  return `${mm}/${dd}/${yyyy}`;  // MM/DD/YYYY — most common on US forms
}

// Per-fill counter for work_authorization Yes/No disambiguation.
// First Yes/No match = eligibility question (→ Yes), second = sponsorship (→ No).
// Reset at the start of each autofillPage call.
let _workAuthYesNoCount = 0;

const STATIC_SYNTHETIC = {
  "_yes":        "Yes",
  "_no":         "No",
  "_start_date": "2 weeks",
  "_skip":       null,
  "_heard":      "Company career website",
  "_today":      _todayFormatted(),
  "_pronouns":   "He/Him",
  "_gender":     "Male",        // fuzzyMatchOption alias: male → Man
  "_no_disability": "No",       // direct "No" — simpler and matches most forms
  "_no_veteran":    "No",       // direct "No" instead of long string
  "_na":            "N/A",      // conditional follow-up fields ("if yes, who referred you?" when answer is No)
  "_full_time":     "Full-time", // employment type
};

// Common term aliases for fuzzy matching (handles "Male"→"Man", "Female"→"Woman", etc.)
const OPTION_ALIASES = {
  'male':   'man',
  'female': 'woman',
  'm':      'man',
  'f':      'woman',
  'prefer not to disclose': 'prefer not to disclose',
  'decline to state':       'prefer not to disclose',
  'i prefer not to answer': 'prefer not to disclose',
};

function fuzzyMatchOption(value, options) {
  if (!options || !options.length) return null;
  const target = value.toLowerCase().trim();
  // 1. Exact match
  let m = options.find(o => o.toLowerCase().trim() === target);
  if (m) return m;
  // 2. Substring match — option contains target (not the reverse: avoids "woman" matching "man")
  m = options.find(o => o.toLowerCase().includes(target));
  if (m) return m;
  // 3. Alias table (male→man, female→woman, etc.) — exact match only
  // substring check MUST NOT be used here: "woman".includes("man") is true,
  // which would return "Woman" when looking for alias "man".
  const alias = OPTION_ALIASES[target];
  if (alias) {
    m = options.find(o => o.toLowerCase().trim() === alias);
    if (m) return m;
  }
  return null;
}

function normalizeLabel(text) {
  return text.toLowerCase().trim()
    .replace(/[^\w\s]/g, '')   // strip ALL non-word, non-space chars (handles ✱ ❋ ＊ etc.)
    .replace(/\s+/g, '_')
    .replace(/_+/g, '_')
    .trim();
}

// ─── Tier 0: indexed experience/education block resolver ─────────────────────
// Handles Greenhouse/Workday field names like:
//   job_application[work_experiences_attributes][0][company]
//   job_application[education_attributes][1][discipline_id]
// Returns the correct profile value for that block index, or _TIER0_MISS to fall through.
const _TIER0_MISS = Symbol('tier0_miss');

function resolveIndexedSection(field, profile) {
  const name = field.name || '';
  if (!name) return _TIER0_MISS;

  // Helper: strip parens and underscores from sub-key for easy matching
  // 'start_date(1i)' → 'startdate1i',  'school_name_id' → 'schoolnameid'
  function cleanKey(raw) {
    return raw.toLowerCase().replace(/[()]/g, '').replace(/_/g, '').trim();
  }

  // ── Work experience block ──────────────────────────────────────────────────
  const workM = name.match(/(?:work[_\s]*experience(?:[s]?(?:[_\s]*attributes)?)|employment[s]?)\]\[(\d+)\]\[([^\]]+)/i);
  if (workM) {
    const idx = parseInt(workM[1]);
    const sub = cleanKey(workM[2]);
    const job = (profile.work_history || [])[idx];
    if (!job) return null; // index out of range — leave blank
    if (/^company$|^employer$|^org$|companyname|employername/.test(sub)) return job.company || '';
    if (/^title$|^position$|^role$|jobtitle|positiontitle/.test(sub))    return job.title   || '';
    if (/description|summary|responsibilit|duties/.test(sub))            return job.description || '';
    if (/startdate1i|startyear/.test(sub))  return job.start_year  || '';
    if (/startdate2i|startmonth/.test(sub)) return job.start_month || '';
    if (/enddate1i|endyear/.test(sub))      return job.current ? '' : (job.end_year  || '');
    if (/enddate2i|endmonth/.test(sub))     return job.current ? '' : (job.end_month || '');
    if (/currentemployer|stillwork|present|iscurrent/.test(sub)) return job.current ? '1' : '0';
    if (/^location$|^city$/.test(sub))      return job.location || '';
    return _TIER0_MISS; // unknown sub-key — let later tiers handle
  }

  // ── Education block ────────────────────────────────────────────────────────
  const eduM = name.match(/education(?:[s]?(?:[_\s]*attributes?)?)?\]\[(\d+)\]\[([^\]]+)/i);
  if (eduM) {
    const idx = parseInt(eduM[1]);
    const sub = cleanKey(eduM[2]);
    const edu = (profile.education_history || [])[idx];
    if (!edu) return null;
    if (/schoolname|schoolid|institutionname|universityname|^school$/.test(sub)) return edu.school || '';
    if (/^degree$|^degreeid$|degreelevel|degreetype/.test(sub)) {
      // Return just the degree-level word — fillSelect partial match handles full option text
      const deg = edu.degree || '';
      if (/master/i.test(deg))    return "Master's";
      if (/bachelor/i.test(deg))  return "Bachelor's";
      if (/doctor|phd/i.test(deg)) return 'Doctoral';
      if (/associate/i.test(deg)) return "Associate's";
      return deg;
    }
    if (/discipline|fieldofstudy|major|concentration|^field$/.test(sub)) return edu.field || '';
    if (/enddate1i|endyear|graduationyear/.test(sub))   return edu.end_year   || '';
    if (/enddate2i|endmonth/.test(sub))                  return edu.end_month  || '';
    if (/startdate1i|startyear/.test(sub))               return edu.start_year || '';
    if (/startdate2i|startmonth/.test(sub))              return edu.start_month || '';
    if (/^gpa$|gradepoint/.test(sub))                    return edu.gpa || '';
    return _TIER0_MISS;
  }

  return _TIER0_MISS;
}

function resolveAnswer(field, profile, memory) {
  // Tier 0: indexed work/education block (Greenhouse pattern [section][N][sub_key])
  const t0 = resolveIndexedSection(field, profile);
  if (t0 !== _TIER0_MISS) return t0;

  let label = field.label_text.toLowerCase().trim();

  // For Lever UUID text fields where getLabel fell back to the field name,
  // walk up the DOM to find the real question text before matching.
  if (/^cards\[[0-9a-f-]{8,}\]\[field\d+\]|^surveys\[/i.test(label)) {
    try {
      const el = document.querySelector(field.selector);
      if (el) {
        // Walk up at most 8 ancestors; at each level scan for any non-option label
        let p = el.parentElement;
        for (let d = 0; d < 8 && p && p !== document.body; d++, p = p.parentElement) {
          // All labels in this ancestor's direct or nested children
          const lbls = p.querySelectorAll('label');
          for (const lbl of lbls) {
            // Skip radio/checkbox option wrappers
            if (lbl.querySelector('input[type=radio],input[type=checkbox]')) continue;
            const clone = lbl.cloneNode(true);
            clone.querySelectorAll('input,select,textarea,button').forEach(c => c.remove());
            const t = (clone.innerText || clone.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
            if (t && t.length > 8 && t !== label) { label = t; break; }
          }
          if (label !== field.label_text.toLowerCase().trim()) break;
          // Also check heading text (h1-h4) as question label
          for (const hTag of ['h4','h3','h2','h1']) {
            const h = p.querySelector(':scope > ' + hTag);
            if (h) {
              const t = (h.innerText || h.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
              if (t && t.length > 8 && t !== label) { label = t; break; }
            }
          }
          if (label !== field.label_text.toLowerCase().trim()) break;
        }
      }
    } catch(e) {}
  }

  const stripped = label.replace(/[^\w\s]/g, '').replace(/\s+/g, ' ').trim();

  // Tier 1: alias table
  for (const probe of [label, stripped]) {
    if (!(probe in LABEL_ALIASES)) continue;
    const profileKey = LABEL_ALIASES[probe];
    if (profileKey === null) return null; // explicitly skip
    // Support nested keys like "skills_by_category.languages"
    if (profileKey.includes('.')) {
      const [parent, child] = profileKey.split('.');
      return profile[parent]?.[child] ?? null;
    }
    const mappedKey = PROFILE_KEY_MAP[profileKey] || profileKey;
    const val = profile[mappedKey] ?? profile[profileKey];
    // portfolio_url falls back to github_url if empty (website field should show GitHub if no portfolio)
    if ((profileKey === 'portfolio_url' || mappedKey === 'portfolio_url') && !val) {
      return profile.github_url || null;
    }
    return val ?? null;
  }

  // Tier 2: QUESTION_BANK regex — test both raw label AND stripped (handles "Phone *" → "phone")
  for (const { re, key } of QUESTION_BANK) {
    if (re.test(label) || re.test(stripped) || re.test(field.placeholder)) {
      if (key === '_claude') return '_needs_claude';
      if (key in STATIC_SYNTHETIC) {
        const synth = STATIC_SYNTHETIC[key];
        if (synth === null) return null; // _skip
        // Always fuzzy-match against available options (handles Male→Man, Yes→Ja, etc.)
        if (field.select_options?.length || field.radio_options?.length) {
          const opts = field.select_options?.length ? field.select_options : field.radio_options;
          // _heard: "Company career website" rarely matches dropdown options exactly.
          // Try several candidate strings in priority order until one matches.
          if (key === '_heard') {
            const heardCandidates = [
              profile.referral_source || '',
              'Company website', 'Company career website', 'Career website',
              'Company career page', 'Career page', 'Company site',
              'Website', 'Job board', 'LinkedIn', 'Indeed', 'Other',
            ];
            for (const c of heardCandidates) {
              if (!c) continue;
              const m = fuzzyMatchOption(c, opts);
              if (m) return m;
            }
            // nothing matched — return first non-blank option as last resort
            return opts.find(o => o.trim()) ?? synth;
          }
          // _gender: hardcoded Male — explicit multi-candidate search, never Female.
          if (key === '_gender') {
            return opts.find(o => /^male$/i.test(o.trim()))
                || opts.find(o => /^man$/i.test(o.trim()))
                || opts.find(o => /\bmale\b/i.test(o) && !/female/i.test(o))
                || opts.find(o => /\bman\b/i.test(o) && !/woman/i.test(o))
                || opts.find(o => /^m$/i.test(o.trim()))
                || 'Male';
          }
          return fuzzyMatchOption(synth, opts) ?? synth;
        }
        return synth;
      }
      const mappedKey = PROFILE_KEY_MAP[key] || key;
      const val = profile[mappedKey] ?? profile[key];
      if (val === undefined || val === null) return null;
      // For work_authorization: if the field has options that include "authorized to work" and "sponsorship",
      // always pick the "authorized" option (profile may not match option text exactly)
      if (key === 'work_authorization') {
        const opts = field.select_options?.length ? field.select_options
                   : field.radio_options?.length  ? field.radio_options : null;
        if (opts) {
          // Verbose option with "authorized to work" text (Greenhouse/Workday style)
          const authOpt = opts.find(o => /authorized.{0,20}work|do not require.{0,20}sponsor/i.test(o));
          if (authOpt) return authOpt;
          // Yes/No radio (Lever work-auth section gives both questions the same section label).
          // Distinguish strategy:
          //   1. field index in name: field0 = eligible (→ Yes), field1 = sponsorship (→ No)
          //   2. counter fallback: first encounter → Yes, second → No
          const isYesNo = opts.some(o => /^yes$/i.test(o.trim()));
          if (isYesNo) {
            const yesOpt = opts.find(o => /^yes$/i.test(o.trim()));
            const noOpt  = opts.find(o => /^no$/i.test(o.trim()));
            const fieldIdxMatch = (field.group_name || field.name || '').match(/\[field(\d+)\]/i);
            if (fieldIdxMatch) {
              const fieldIdx = parseInt(fieldIdxMatch[1]);
              // field0 = "eligible to work?" → Yes — but ONLY for the first such group.
              // A second field0 (different UUID card) is a different question (e.g. referral) → No.
              if (fieldIdx === 0 && _workAuthYesNoCount === 0) {
                _workAuthYesNoCount++;
                return yesOpt || opts[0];
              }
              _workAuthYesNoCount++;
              return noOpt || opts[opts.length - 1];
            }
            // No field index — use order-of-encounter counter
            _workAuthYesNoCount++;
            return _workAuthYesNoCount === 1 ? (yesOpt || opts[0]) : (noOpt || opts[opts.length - 1]);
          }
          return fuzzyMatchOption(String(val), opts) ?? String(val);
        }
      }
      return String(val);
    }
  }

  // Tier 3: memory lookup — exact then fuzzy token overlap
  // Guard: skip memory for labels shorter than 5 chars (e.g. "no", "yes", "ok")
  // Also skip if memory answer is a "decline/prefer not" placeholder — let Tier 3.5 pick real EEO value
  // Also skip for UUID survey fields (name like surveys[responses][uuid][field0]) — stale EEO cache
  const isUUIDSurvey = /surveys.*responses.*field/i.test(field.group_name || '')
                    || /surveys.*responses.*field/i.test(label)
                    || /cards[0-9a-f]{8,}field\d+/i.test(normalizeLabel(field.group_name || field.label_text || ''));
  const memKey = normalizeLabel(label);
  if (!isUUIDSurvey && memKey.length >= 5 && memory[memKey]) {
    const memAns = memory[memKey].answer;
    if (!/prefer not|decline|not wish to answer|not want to/i.test(String(memAns))) {
      return memAns;
    }
  }
  const fuzzyHit = (!isUUIDSurvey && memKey.length >= 5) ? fuzzyMemoryLookup(label, memory) : null;
  if (fuzzyHit) {
    const fuzzyAns = fuzzyHit.answer;
    if (!/prefer not|decline|not wish to answer|not want to/i.test(String(fuzzyAns))) {
      return fuzzyAns;
    }
  }

  // Tier 3.5: Infer EEO field type from options content (select_options OR radio_options).
  // Lever EEO survey uses radio groups with UUID names — options live in radio_options, not select_options.
  const _eeoOpts = (field.select_options && field.select_options.length > 0)
    ? field.select_options
    : (field.radio_options && field.radio_options.length > 0 ? field.radio_options : null);
  if (_eeoOpts) {
    console.log('[T3.5]', (field.group_name||label).slice(0,50), '| opts:', JSON.stringify(_eeoOpts.slice(0,6)));
    const optText = _eeoOpts.map(o => o.toLowerCase()).join(' ');
    let guess = null;
    // Gender: has "woman" OR "female" OR "non-binary" — but NOT veteran/disability keywords
    if (/\bwoman\b|\bnon.?binary\b|\bgenderqueer\b|\btransgender\b|\bfemale\b/.test(optText)
        && !/\bveteran\b|\bdisabilit\b|\bprotected\b/.test(optText)) {
      const genderVal = profile.eeo_gender || profile.gender || 'Male';
      guess = fuzzyMatchOption(genderVal, _eeoOpts);
      console.log('[T3.5 gender] genderVal:', genderVal, '| fuzz result:', guess);
      // Safety: if we got "Woman"/"Female" but eeo_gender is Male, something went wrong — skip
      if (guess && /woman|female/i.test(guess) && /male|man/i.test(genderVal)) { console.log('[T3.5 gender] safety discard'); guess = null; }
    }
    // Ethnicity: specific racial/ethnic group names
    if (!guess && /\basian\b|\bhispanic\b|\bamerican indian\b|\bnative hawaiian\b|\bblack or african\b|\btwo or more races\b|\bblack or black british\b|\bmixed\b/.test(optText)) {
      // UK forms use "Asian/Asian British: Indian" style — prefer specific subcategory over "Any other"
      const isUKForm = /\bbritish\b/.test(optText);
      const ethnValSpecific = isUKForm ? (profile.eeo_ethnicity_uk || 'Indian') : null;
      const ethnValBase    = profile.eeo_ethnicity || 'Asian';
      // Try specific first (e.g. "Indian" → "Asian/Asian British: Indian"), then fall to base
      if (ethnValSpecific) guess = fuzzyMatchOption(ethnValSpecific, _eeoOpts);
      if (!guess) guess = fuzzyMatchOption(ethnValBase, _eeoOpts);
      // If base matched "Any other" but a more specific option exists, prefer it
      if (guess && /any other/i.test(guess) && ethnValSpecific) {
        const betterGuess = fuzzyMatchOption(ethnValSpecific, _eeoOpts);
        if (betterGuess && !/any other/i.test(betterGuess)) guess = betterGuess;
      }
    }
    // Sexual orientation: heterosexual, gay, bisexual, lesbian — prefer "Heterosexual/Straight"
    if (!guess && /\bheterosexual\b|\bgay\b|\bbisexual\b|\blesbian\b/.test(optText)
        && !/\bgender identity\b|\bethnicit\b|\brace\b|\bveteran\b/.test(optText)) {
      guess = _eeoOpts.find(o => /heterosexual|straight/i.test(o))
           || _eeoOpts.find(o => /prefer not|decline|choose not/i.test(o));
    }
    // Veteran: options OR label has "veteran"/"military" keywords — not gender/ethnicity
    const labelHasVeteran = /\bveteran\b|\bmilitary.{0,15}(service|served|status)|served.{0,20}military/i.test(label);
    if (!guess && (/\bveteran\b|\bmilitary service\b|\bprotected veteran\b/.test(optText) || labelHasVeteran)
        && !/\bgender\b|\bethnicit\b|\brace\b|\bwoman\b/.test(optText)) {
      guess = _eeoOpts.find(o => /not.{0,10}(a |protected )?veteran|i am not/i.test(o))
           || fuzzyMatchOption('No', _eeoOpts);
    }
    // Disability: options OR label has "disability" keywords — not gender/ethnicity
    const labelHasDisability = /\bdisabilit\b|\bdisabled\b/i.test(label);
    if (!guess && (/\bdisabilit\b|\bdisabled\b/.test(optText) || labelHasDisability)
        && !/\bgender\b|\bethnicit\b|\brace\b/.test(optText)) {
      guess = _eeoOpts.find(o => /no.{0,5}(,|i )?.{0,20}(don.t|do not|without|not have).{0,20}disabilit|i don.t have/i.test(o))
           || fuzzyMatchOption('No', _eeoOpts);
    }
    // Unknown survey type — if has Yes/No (e.g. veteran/disability 3-option), default "No"
    // (covers "Are you a veteran?" when UUID label has no keywords and options have none either)
    if (!guess && isUUIDSurvey) {
      const hasYes = _eeoOpts.some(o => /^yes$/i.test(o.trim()));
      const hasNo  = _eeoOpts.some(o => /^no$/i.test(o.trim()));
      if (hasYes && hasNo) {
        guess = _eeoOpts.find(o => /^no$/i.test(o.trim()));
      }
    }
    // Last resort: pick "prefer not to disclose" if nothing matched
    if (!guess && /prefer not|decline|choose not|wish not/i.test(optText)) {
      guess = _eeoOpts.find(o => /prefer not|decline|choose not|wish not/i.test(o)) || null;
    }
    if (guess) return guess;
  }

  // Tier 4: field-type fallback
  if (field.field_type === 'email') return profile.email || null;
  if (field.field_type === 'tel') return profile.phone || null;
  if (field.field_type === 'url') return profile.linkedin_url || profile.github_url || null;

  return '_needs_claude';
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function isTruthy(value) {
  return /^(yes|true|1|on|checked)$/i.test(String(value).trim());
}

const _MONTH_NAMES = ['january','february','march','april','may','june',
                      'july','august','september','october','november','december'];
const _MONTH_ABBR  = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'];

// US state full-name → abbreviation and vice-versa
const _STATE_TO_ABBR = {
  'alabama':'al','alaska':'ak','arizona':'az','arkansas':'ar','california':'ca',
  'colorado':'co','connecticut':'ct','delaware':'de','florida':'fl','georgia':'ga',
  'hawaii':'hi','idaho':'id','illinois':'il','indiana':'in','iowa':'ia','kansas':'ks',
  'kentucky':'ky','louisiana':'la','maine':'me','maryland':'md','massachusetts':'ma',
  'michigan':'mi','minnesota':'mn','mississippi':'ms','missouri':'mo','montana':'mt',
  'nebraska':'ne','nevada':'nv','new hampshire':'nh','new jersey':'nj','new mexico':'nm',
  'new york':'ny','north carolina':'nc','north dakota':'nd','ohio':'oh','oklahoma':'ok',
  'oregon':'or','pennsylvania':'pa','rhode island':'ri','south carolina':'sc',
  'south dakota':'sd','tennessee':'tn','texas':'tx','utah':'ut','vermont':'vt',
  'virginia':'va','washington':'wa','west virginia':'wv','wisconsin':'wi','wyoming':'wy',
};
const _ABBR_TO_STATE = Object.fromEntries(Object.entries(_STATE_TO_ABBR).map(([k,v]) => [v,k]));

function fillSelect(el, value) {
  const target = value.toLowerCase().trim();
  const opts = Array.from(el.options);
  // exact match first
  let opt = opts.find(o => o.text.toLowerCase().trim() === target);
  // partial match
  if (!opt) opt = opts.find(o => o.text.toLowerCase().includes(target) || target.includes(o.text.toLowerCase().trim()));
  // value attribute match
  if (!opt) opt = opts.find(o => o.value.toLowerCase() === target);

  // Month number → name (e.g. Greenhouse [start_date(2i)] returns "01" but opts have "January")
  if (!opt && /^(0?[1-9]|1[0-2])$/.test(value.trim())) {
    const mIdx = parseInt(value.trim()) - 1;
    const mName = _MONTH_NAMES[mIdx];
    const mAbbr = _MONTH_ABBR[mIdx];
    opt = opts.find(o => o.text.toLowerCase().trim() === mName)
       || opts.find(o => o.text.toLowerCase().startsWith(mAbbr))
       || opts.find(o => o.value.toLowerCase() === mName)
       || opts.find(o => o.value === String(mIdx + 1))   // some selects use "1"–"12"
       || opts.find(o => o.value === String(mIdx + 1).padStart(2, '0')); // or "01"–"12"
  }

  // State full name ↔ abbreviation bidirectional mapping
  if (!opt) {
    // Try "New York" → "NY" (for dropdowns with abbreviation options)
    const abbr = _STATE_TO_ABBR[target];
    if (abbr) {
      opt = opts.find(o => o.text.toLowerCase().trim() === abbr)
         || opts.find(o => o.value.toLowerCase() === abbr);
    }
  }
  if (!opt) {
    // Try "NY" → "New York" (for dropdowns with full-name options)
    const full = _ABBR_TO_STATE[target];
    if (full) {
      opt = opts.find(o => o.text.toLowerCase().trim() === full)
         || opts.find(o => o.value.toLowerCase() === full);
    }
  }

  // Country select fallback: if 50+ options and looks like a country list, use United States
  if (!opt && opts.length > 50 && opts.some(o => /^united states$/i.test(o.text.trim()))) {
    opt = opts.find(o => /^united states$/i.test(o.text.trim()));
  }
  if (opt) {
    // Native property setter bypasses React's synthetic override so React state actually updates
    try {
      const desc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
      if (desc && desc.set) desc.set.call(el, opt.value);
      else el.value = opt.value;
    } catch(e) { el.value = opt.value; }
    el.dispatchEvent(new Event('input',  { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
}

function _clickRadioInput(r) {
  // Step 1: use native checked setter to bypass React's property override
  try {
    const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked');
    if (desc && desc.set) desc.set.call(r, true);
  } catch(e) { r.checked = true; }

  // Step 2: fire events — mousedown/mouseup/click order triggers React synthetic handlers
  r.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
  r.dispatchEvent(new MouseEvent('mouseup',   { bubbles: true, cancelable: true }));

  // Step 3: prefer clicking the associated label (visually hidden inputs — Ashby, Lever EEO)
  if (r.id) {
    const lbl = document.querySelector(`label[for="${r.id}"]`);
    if (lbl) { lbl.click(); return; }
  }
  const parentLbl = r.closest('label');
  if (parentLbl) { parentLbl.click(); return; }

  // Fallback: direct click + change on the input itself
  r.dispatchEvent(new MouseEvent('click',  { bubbles: true, cancelable: true }));
  r.dispatchEvent(new Event('change', { bubbles: true }));
  r.dispatchEvent(new Event('input',  { bubbles: true }));
}

function _getRadioLabel(r, domRoot) {
  // 1. label[for=id]
  if (r.id) {
    const lbl = (domRoot || document).querySelector(`label[for="${r.id}"]`);
    if (lbl) return (lbl.innerText || lbl.textContent || '').toLowerCase().trim();
  }
  // 2. aria-label
  const al = r.getAttribute('aria-label');
  if (al) return al.toLowerCase().trim();
  // 3. parent label
  const pl = r.closest('label');
  if (pl) return (pl.innerText || pl.textContent || '').toLowerCase().trim();
  // 4. value attribute
  return (r.value || '').toLowerCase().trim();
}

function fillRadio(field, value) {
  // Use JS property comparison as fallback — CSS selector with brackets in name can silently fail
  let radios = Array.from(document.querySelectorAll(`input[type="radio"][name="${field.group_name}"]`));
  if (!radios.length && field.group_name) {
    radios = Array.from(document.querySelectorAll('input[type="radio"]'))
      .filter(r => r.name === field.group_name);
  }
  if (!radios.length) return;

  const entries = radios.map(r => ({ el: r, lbl: _getRadioLabel(r) }));
  const labels  = entries.map(e => e.lbl);

  // Pass 1: exact match on label text or value attribute
  const target = value.toLowerCase().trim();
  let entry = entries.find(e => e.lbl === target || e.el.value.toLowerCase() === target);
  if (entry) { _clickRadioInput(entry.el); return; }

  // Pass 2: fuzzyMatchOption — runs exact-first across ALL options, avoids "woman".includes("man")
  const bestLbl = fuzzyMatchOption(value, labels);
  if (bestLbl !== null) {
    entry = entries.find(e => e.lbl === bestLbl);
    if (entry) { _clickRadioInput(entry.el); return; }
  }

  // Pass 3: value-attribute fuzzy match (for radio buttons whose label detection failed)
  entry = entries.find(e => e.el.value.toLowerCase().includes(target) && !e.el.value.toLowerCase().includes('prefer'));
  if (entry) { _clickRadioInput(entry.el); return; }

  // Pass 4: index-based match using field.radio_options (form-detector text labels)
  // Lever EEO radio buttons have UUID value attributes — _getRadioLabel falls back to UUID,
  // making text matching fail. Map by index using the labels form-detector collected.
  const roLabels = (field.radio_options || []).map(o =>
    (typeof o === 'string' ? o : (o.label || o.value || '')).toLowerCase().trim()
  );
  if (roLabels.length === radios.length) {
    const bestRoLbl = fuzzyMatchOption(value, roLabels);
    if (bestRoLbl !== null) {
      const idx = roLabels.indexOf(bestRoLbl);
      if (idx >= 0) { _clickRadioInput(radios[idx]); return; }
    }
  }

  // Fallback: isTruthy → first only (never pick "last" — it's often "Prefer not to disclose")
  // Hard rule: if filling "Male"/"Man", NEVER fall back to a Female/Woman option.
  console.warn('[fillRadio] no match', JSON.stringify({ value, labels, roLabels }));
  if (isTruthy(value)) {
    const isMaleTarget = /^(male|man|m)$/i.test(value.trim());
    const safeFirst = isMaleTarget
      ? radios.find(r => !/^(female|woman|f)$/i.test((r.value || '').trim()) &&
                         !/^(female|woman)$/i.test((_getRadioLabel(r) || '').trim()))
      : radios[0];
    if (safeFirst) _clickRadioInput(safeFirst);
    return;
  }
}

function fillAriaRadio(field, value) {
  const target = value.toLowerCase().trim();
  // Find the radiogroup by group_name label in the DOM
  const groups = document.querySelectorAll('[role="radiogroup"]');
  for (const rg of groups) {
    const rgLabel = (rg.getAttribute('aria-label') || rg.innerText || '').toLowerCase();
    if (!rgLabel.includes(field.group_name.toLowerCase().slice(0, 15))) continue;
    const options = rg.querySelectorAll('[role="radio"]');
    for (const opt of options) {
      const optText = (opt.getAttribute('aria-label') || opt.innerText || opt.textContent || '').toLowerCase().trim();
      if (optText === target || optText.includes(target) || target.includes(optText)) {
        opt.click();
        opt.dispatchEvent(new Event('change', { bubbles: true }));
        return;
      }
    }
    // Fallback: yes/truthy → first, no → last
    if (options.length > 0) {
      if (isTruthy(value)) { options[0].click(); return; }
      if (/^no$/i.test(value.trim())) { options[options.length - 1].click(); return; }
    }
  }
}

async function uploadFile(el) {
  // Resume upload via service_worker. 5 s timeout prevents hanging the auto-apply loop.
  let result;
  try {
    const msgP = chrome.runtime.sendMessage({ type: 'GET_RESUME_B64' });
    const timeoutP = new Promise(r => setTimeout(() => r(null), 5000));
    result = await Promise.race([msgP, timeoutP]);
  } catch(e) { return; }
  if (!result || !result.b64) return;

  const byteChars = atob(result.b64);
  const bytes = new Uint8Array(byteChars.length);
  for (let i = 0; i < byteChars.length; i++) bytes[i] = byteChars.charCodeAt(i);
  const blob = new Blob([bytes], { type: 'application/pdf' });
  const file = new File([blob], 'Pranav_Pradhan_resume.pdf', { type: 'application/pdf' });
  const dt = new DataTransfer();
  dt.items.add(file);
  el.files = dt.files;
  el.dispatchEvent(new Event('change', { bubbles: true }));
  el.dispatchEvent(new InputEvent('input', { bubbles: true }));
}

async function _fillText(el, value) {
  el.focus();
  // React native setter trick — signals React that value changed
  const proto = el instanceof HTMLTextAreaElement
    ? window.HTMLTextAreaElement.prototype
    : window.HTMLInputElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, 'value');
  if (desc?.set) {
    desc.set.call(el, '');
    desc.set.call(el, value);
  } else {
    el.value = value;
  }
  el.dispatchEvent(new InputEvent('input',  { bubbles: true, composed: true, data: value }));
  el.dispatchEvent(new Event('change',       { bubbles: true }));

  // If React didn't accept the value (controlled component with custom handler),
  // fall back to execCommand — selects all then inserts text as if typed
  if (el.value !== value) {
    el.select?.();
    try {
      document.execCommand('selectAll', false, null);
      document.execCommand('insertText', false, value);
    } catch(e) {}
    // Still didn't stick? Fire key events one char at a time (last resort)
    if (el.value !== value) {
      if (desc?.set) desc.set.call(el, '');
      else el.value = '';
      el.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true }));
      for (const char of value) {
        el.dispatchEvent(new KeyboardEvent('keydown',  { key: char, bubbles: true }));
        el.dispatchEvent(new KeyboardEvent('keypress', { key: char, bubbles: true }));
        if (desc?.set) desc.set.call(el, el.value + char);
        else el.value += char;
        el.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true, data: char }));
        el.dispatchEvent(new KeyboardEvent('keyup', { key: char, bubbles: true }));
      }
    }
  }
  el.dispatchEvent(new Event('blur', { bubbles: true }));
}

async function fillAutocomplete(el, value) {
  // Google Places / typeahead — must simulate real typing to trigger API lookup.
  // execCommand('insertText') is the closest to native input that Places accepts.
  el.focus();

  // Clear any existing value first
  const proto = el instanceof HTMLTextAreaElement
    ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, 'value');
  if (desc?.set) { desc.set.call(el, ''); }
  else { el.value = ''; }
  el.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true }));

  // Type character-by-character with real KeyboardEvents so Places API triggers
  const typeValue = value; // use full "New York, NY" not just "New York"
  for (const char of typeValue) {
    const code = char.charCodeAt(0);
    el.dispatchEvent(new KeyboardEvent('keydown',  { key: char, keyCode: code, which: code, bubbles: true, composed: true }));
    el.dispatchEvent(new KeyboardEvent('keypress', { key: char, keyCode: code, which: code, bubbles: true, composed: true }));
    // Append the character using native setter so React sees it
    if (desc?.set) desc.set.call(el, el.value + char);
    else el.value += char;
    el.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true, data: char, inputType: 'insertText' }));
    el.dispatchEvent(new KeyboardEvent('keyup',    { key: char, keyCode: code, which: code, bubbles: true, composed: true }));
    await sleep(60); // realistic typing speed
  }

  // Also try execCommand as a parallel path (works in some contexts)
  try {
    el.select?.();
    document.execCommand('insertText', false, typeValue);
  } catch(e) {}

  // Wait for Places API response (needs network round-trip)
  await sleep(2000);

  const dropdownSel = [
    '.pac-item', '[role="option"]', '[role="listbox"] [role="option"]',
    '.suggestions li', '.autocomplete-suggestion', '[data-testid*="suggestion"]',
    '[class*="suggestion"]', '[class*="autocomplete"] li',
  ].join(', ');
  const first = document.querySelector(dropdownSel);
  if (first) {
    first.click();
    await sleep(400);
    return;
  }

  // ArrowDown + Enter to accept first suggestion (keyboard-only forms)
  el.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', keyCode: 40, which: 40, bubbles: true }));
  await sleep(400);
  el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', keyCode: 13, which: 13, bubbles: true }));
  await sleep(300);

  // Last resort: force the raw value if field is still empty
  if (!el.value || el.value.trim().length < 2) {
    if (desc?.set) { desc.set.call(el, value); }
    else { el.value = value; }
    el.dispatchEvent(new InputEvent('input', { bubbles: true, composed: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
}

async function fillField(field, value) {
  let el = document.querySelector(field.selector);
  if (!el) {
    // XPath fallback
    try {
      const xr = document.evaluate(
        '//*[@id="' + field.id + '"]', document, null,
        XPathResult.FIRST_ORDERED_NODE_TYPE, null
      );
      el = xr.singleNodeValue;
    } catch(e) {}
  }
  // Radio/aria_radio fills use field.group_name, not el — don't abort if selector fails.
  const isRadioType = field.field_type === 'radio' || field.field_type === 'aria_radio';
  if (!el && !isRadioType) return false;

  switch (field.field_type) {
    case 'text':
    case 'email':
    case 'tel':
    case 'url':
    case 'number':
    case 'search':
    case 'textarea': {
      // Detect Places / typeahead autocomplete widgets — they require typing + selecting from dropdown
      const isAutocomplete =
        el.getAttribute('autocomplete') === 'off' ||
        el.getAttribute('role') === 'combobox' ||
        el.getAttribute('aria-autocomplete') === 'list' ||
        el.getAttribute('aria-autocomplete') === 'both' ||
        el.getAttribute('aria-haspopup') === 'listbox' ||
        el.className.toLowerCase().includes('location') ||
        (el.placeholder || '').toLowerCase().includes('city') ||
        (el.name || '').toLowerCase().includes('location');
      if (isAutocomplete) {
        await fillAutocomplete(el, value);
      } else {
        await _fillText(el, value);
      }
      break;
    }
    case 'select':
      fillSelect(el, value);
      break;
    case 'radio':
      fillRadio(field, value);
      break;
    case 'aria_radio':
      fillAriaRadio(field, value);
      break;
    case 'checkbox': {
      const wantChecked = isTruthy(value);
      if (el.checked !== wantChecked) {
        // Use click() — triggers React's synthetic onClick/onChange (direct el.checked= is ignored by React)
        el.click();
      }
      el.dispatchEvent(new Event('change', { bubbles: true }));
      break;
    }
    case 'file':
      await uploadFile(el);
      break;
  }
  await sleep(100 + Math.random() * 80);
  return true;
}

async function autofillPage(profile, memory, claudeAnswers) {
  _workAuthYesNoCount = 0; // reset per-page disambiguator
  const fields = detectFormFields();
  const needsClaude = [];
  const results = { filled: 0, skipped: 0, claude: 0, fields: [] };

  for (const f of fields) {
    // File inputs: upload resume — but skip cover letter file fields (not a PDF substitute)
    if (f.field_type === 'file') {
      const lbl = (f.label_text || '').toLowerCase();
      if (/cover.{0,10}letter|covering.{0,10}letter/.test(lbl)) {
        results.skipped++;
        results.fields.push({ label: f.label_text || 'cover letter', value: '(skipped — upload manually)', source: 'skip' });
        continue;
      }
      await fillField(f, '_file');
      results.filled++;
      results.fields.push({ label: f.label_text || 'resume', value: '📎 resume.pdf', source: 'storage' });
      continue;
    }

    // Skip pre-filled text/textarea/autocomplete — but ALWAYS re-process selects/radios/checkboxes
    // because their "current_value" may just be a placeholder default ("Prefer not to disclose", etc.)
    if (f.current_value && f.current_value.length > 2 &&
        f.field_type !== 'radio' && f.field_type !== 'checkbox' &&
        f.field_type !== 'select' && f.field_type !== 'aria_radio') {
      results.skipped++;
      continue;
    }

    const ans = resolveAnswer(f, profile, memory);
    if (ans === '_needs_claude') {
      needsClaude.push(f);
      continue;
    }
    // Skip null, empty string, or whitespace-only answers — don't clear existing values
    if (ans === null || String(ans).trim() === '') {
      results.skipped++;
      continue;
    }
    const ok = await fillField(f, ans);
    if (ok) {
      results.filled++;
      results.fields.push({ label: f.label_text, value: ans, source: 'profile' });
    }
  }

  // Fill Claude-answered fields
  if (claudeAnswers) {
    for (const f of needsClaude) {
      const memKey = normalizeLabel(f.label_text);
      const ans = claudeAnswers[memKey] || claudeAnswers[f.label_text];
      if (ans) {
        const ok = await fillField(f, ans);
        if (ok) {
          results.claude++;
          results.fields.push({ label: f.label_text, value: ans, source: 'claude' });
        }
      }
    }
  }

  // Direct portal fill — catches fields form_detector missed + React-cleared values
  const directCount = await directPortalFill(profile);
  if (directCount > 0) results.filled += directCount;

  results.needsClaude = needsClaude;
  return results;
}

function buildFallbackAnswers(fields, profile, memory) {
  const answers = {};
  for (const f of fields) {
    const ans = resolveAnswer(f, profile, memory);
    if (ans && ans !== '_needs_claude') {
      answers[normalizeLabel(f.label_text)] = ans;
    }
  }
  return answers;
}

// ─── Direct portal fill ───────────────────────────────────────────────────────
// Fallback for fields that form_detector may have missed or that React cleared.
// Runs after the main fill loop. Tries well-known input[name] / input[type] selectors.

const DIRECT_FILL_MAP = [
  // Full name variants
  { selectors: ['input[name="name"]', 'input[name="full_name"]', 'input[name="fullName"]', 'input[autocomplete="name"]'], key: 'full_name' },
  // First / last
  { selectors: ['input[name="first_name"]', 'input[name="firstName"]', 'input[autocomplete="given-name"]'], key: 'first_name' },
  { selectors: ['input[name="last_name"]', 'input[name="lastName"]', 'input[autocomplete="family-name"]'], key: 'last_name' },
  // Email
  { selectors: ['input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]'], key: 'email' },
  // Phone
  { selectors: ['input[type="tel"]', 'input[name="phone"]', 'input[name="phoneNumber"]', 'input[name="phone_number"]'], key: 'phone' },
  // Company
  { selectors: ['input[name="org"]', 'input[name="company"]', 'input[name="current_company"]', 'input[name="currentCompany"]'], key: 'current_company' },
  // LinkedIn / GitHub
  { selectors: ['input[name="linkedin"]', 'input[name="linkedin_url"]', 'input[name="linkedinUrl"]'], key: 'linkedin_url' },
  { selectors: ['input[name="github"]', 'input[name="github_url"]', 'input[name="githubUrl"]'], key: 'github_url' },
  // Portfolio
  { selectors: ['input[name="portfolio"]', 'input[name="portfolio_url"]', 'input[name="website"]'], key: 'portfolio_url' },
];

async function directPortalFill(profile) {
  let count = 0;
  for (const { selectors, key } of DIRECT_FILL_MAP) {
    const value = profile[key];
    if (!value) continue;
    for (const sel of selectors) {
      let el;
      try { el = document.querySelector(sel); } catch(e) { continue; }
      if (!el) continue;
      const t = (el.type || '').toLowerCase();
      if (t === 'hidden' || t === 'password') continue;
      if (el.value && el.value.length > 2) continue; // already filled
      await _fillText(el, value);
      count++;
      break;
    }
  }
  return count;
}

// ─── Fuzzy memory lookup ──────────────────────────────────────────────────────
// When exact normalizeLabel(label) doesn't hit memory, try token overlap.
// Finds the best memory key where ≥80% of label tokens appear in the key or vice-versa.

function fuzzyMemoryLookup(label, memory) {
  const tokens = normalizeLabel(label).split('_').filter(Boolean);
  if (tokens.length === 0) return null;
  let bestScore = 0, bestEntry = null;
  for (const [key, entry] of Object.entries(memory)) {
    const keyTokens = key.split('_').filter(Boolean);
    const overlap = tokens.filter(t => keyTokens.includes(t)).length;
    const score = overlap / Math.max(tokens.length, keyTokens.length);
    if (score >= 0.8 && score > bestScore) {
      bestScore = score;
      bestEntry = entry;
    }
  }
  return bestEntry;
}

// ─── DOM value inspection & retry helpers ─────────────────────────────────────

function getFieldDomValue(field) {
  let el = null;
  try {
    if (field.selector) el = document.querySelector(field.selector);
    if (!el && field.id) el = document.getElementById(field.id);
  } catch(e) {}
  if (!el) return null; // element not in DOM — skip

  if (field.field_type === 'radio') {
    const name = field.group_name || (el.name || '');
    const checked = name
      ? document.querySelector(`input[type="radio"][name="${name}"]:checked`)
      : (el.checked ? el : null);
    return checked ? (checked.value || 'checked') : '';
  }
  if (field.field_type === 'aria_radio') {
    // Consider aria radios as "filled enough" if the field is aria-based — hard to verify
    return 'checked';
  }
  if (field.field_type === 'checkbox') return el.checked ? 'checked' : '';
  if (field.field_type === 'select') {
    if (!el.options || el.options.length === 0) return 'n/a';
    const idx = el.selectedIndex;
    if (idx < 0) return '';
    const opt = el.options[idx];
    // index 0 is usually placeholder "Select..." — treat as empty
    return (opt && opt.value && opt.value !== '' && idx > 0) ? opt.value : '';
  }
  return (el.value || '').trim();
}

function isFieldRequired(field) {
  let el = null;
  try { if (field.selector) el = document.querySelector(field.selector); } catch(e) {}

  // Hidden / conditional field — even if marked required in HTML, skip it.
  // getBoundingClientRect() returns all-zeros when any ancestor has display:none.
  if (el) {
    try {
      const s = window.getComputedStyle(el);
      if (s.display === 'none' || s.visibility === 'hidden') return false;
    } catch(e) {}
    try {
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) return false;
    } catch(e) {}
    // Check ancestor display:none (getComputedStyle doesn't cascade display)
    let p = el.parentElement;
    while (p && p !== document.documentElement) {
      try {
        const ps = window.getComputedStyle(p);
        if (ps.display === 'none' || ps.visibility === 'hidden') return false;
      } catch(e) {}
      if (p.getAttribute && p.getAttribute('aria-hidden') === 'true') return false;
      p = p.parentElement;
    }
  }

  if (/[*✱＊❋★]/.test(field.label_text)) return true;
  if (el && (el.required || el.getAttribute('aria-required') === 'true')) return true;
  return false;
}

// Like resolveAnswer but also falls back to claudeAnswers dict
function resolveAnswerForRetry(field, profile, memory, claudeAnswers) {
  const ans = resolveAnswer(field, profile, memory);
  if (ans !== null && ans !== '_needs_claude') return ans;
  if (claudeAnswers) {
    const key = normalizeLabel(field.label_text);
    return claudeAnswers[key] || claudeAnswers[field.label_text] || null;
  }
  return null;
}
