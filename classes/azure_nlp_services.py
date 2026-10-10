class AzureNLPService:
    def __init__(self, language_client, config):
        self.language_client = language_client
        self.sentiment_escalation_threshold = config["nlp"]["sentiment_analysis"][
            "sentiment_escalation_threshold"
        ]

    def extract_preferences(self, text):
        """Return the user preferences recognised in the text, e.g. {"language": "Polish"}.

        For now only the language (its English name, as returned by Azure AI Language).
        Add further keys here to learn more from the user's messages. A preference that
        could not be recognised is left out, so the stored value is not overwritten.
        """
        preferences = {}

        detected_lang = self._detect_primary_language(text)
        if detected_lang is not None:
            preferences["language"] = detected_lang.name

        return preferences

    def detect_language(self, text):
        """Return the ISO 639-1 code of the text's language, 'en' when it cannot be recognised."""
        detected_lang = self._detect_primary_language(text)
        if detected_lang is None:
            print("[NLP] Defaulting to 'en'")
            return "en"
        return detected_lang.iso6391_name

    def _detect_primary_language(self, text):
        """Return Azure's primary language (name, iso6391_name, confidence_score), or None."""
        print("[NLP] Detecting language...")

        try:
            documents = [{"id": "1", "text": text}]
            result = self.language_client.detect_language(documents=documents)

            # Very short or ambiguous text is returned as "(Unknown)"
            if result and result[0].is_error is False and result[0].primary_language.iso6391_name != "(Unknown)":
                detected_lang = result[0].primary_language
                print(
                    f"[NLP] Language detected: {detected_lang.name} "
                    f"(code: {detected_lang.iso6391_name}, "
                    f"confidence: {detected_lang.confidence_score:.2f})"
                )
                return detected_lang
            else:
                print("[NLP] Language detection failed")
                return None
        except Exception as error:
            print(f"[NLP] Language detection error: {error}")
            return None

    def redact_pii(self, text):
        print("[NLP] Redacting PII (Personally Identifiable Information)...")

        try:
            documents = [{"id": "1", "text": text}]
            result = self.language_client.recognize_pii_entities(documents=documents)

            if result and result[0].is_error is False:
                entities = result[0].entities
                if entities:
                    print(f"[NLP] Found {len(entities)} PII entities to redact")
                    for entity in entities:
                        print(f"  - {entity.category}: {entity.text} → [REDACTED]")

                redacted_text = result[0].redacted_text
                return redacted_text
            else:
                print("[NLP] PII redaction failed, returning original text")
                return text
        except Exception as error:
            print(f"[NLP] PII redaction error: {error}")
            return text

    def extract_entities(self, text):
        print("[NLP] Extracting entities (Named Entity Recognition)...")

        try:
            documents = [{"id": "1", "text": text}]
            result = self.language_client.recognize_entities(documents=documents)

            if result and result[0].is_error is False:
                entities = result[0].entities
                if entities:
                    print(f"[NLP] Found {len(entities)} entities:")
                    for entity in entities:
                        print(
                            f"  - {entity.category}: '{entity.text}' "
                            f"(confidence: {entity.confidence_score:.2f})"
                        )

                # Build tagged text using entity offsets to avoid
                # replacing all occurrences of the same text.
                tagged_text = text
                # Process entities from right-to-left so earlier replacements
                # don't change offsets for later ones.
                sorted_entities = sorted(entities, key=lambda x: x.offset, reverse=True)
                for entity in sorted_entities:
                    start = entity.offset
                    end = start + entity.length
                    replacement = f"[{entity.category.upper()}: {entity.text}]"
                    tagged_text = tagged_text[:start] + replacement + tagged_text[end:]
                return tagged_text
            else:
                print("[NLP] Entity extraction failed")
                return text
        except Exception as error:
            print(f"[NLP] Entity extraction error: {error}")
            return text

    def extract_key_phrases(self, text):
        print("[NLP] Extracting key phrases...")

        try:
            documents = [{"id": "1", "text": text}]
            result = self.language_client.extract_key_phrases(documents=documents)

            if result and result[0].is_error is False:
                key_phrases = result[0].key_phrases
                if key_phrases:
                    print(f"[NLP] Key phrases found: {', '.join(key_phrases)}")
                    return key_phrases
                else:
                    print("[NLP] No key phrases found")
                    return []
            else:
                print("[NLP] Key phrase extraction failed")
                return []
        except Exception as error:
            print(f"[NLP] Key phrase extraction error: {error}")
            return []

    def analyze_sentiment(self, text):
        print("[NLP] Analyzing sentiment...")

        try:
            documents = [{"id": "1", "text": text}]
            result = self.language_client.analyze_sentiment(documents=documents)

            if result and result[0].is_error is False:
                sentiment = result[0].sentiment
                confidence = result[0].confidence_scores
                print(
                    f"[NLP] Sentiment: {sentiment.upper()} "
                    f"positive: {confidence.positive:.2f}, "
                    f"neutral: {confidence.neutral:.2f}, "
                    f"negative: {confidence.negative:.2f})"
                )

                should_escalate = (
                    sentiment == "negative"
                    and confidence.negative > self.sentiment_escalation_threshold
                )
                if should_escalate:
                    print(
                        f"[NLP] ⚠️ High negative sentiment detected "
                        f"(confidence: {confidence.negative:.2f})"
                    )
                    print("[NLP] Recommendation: Escalate to human agent")

                return {
                    "sentiment": sentiment,
                    "confidence": confidence,
                    "should_escalate": should_escalate,
                }
            else:
                print("[NLP] Sentiment analysis failed")
                return {
                    "sentiment": "neutral",
                    "confidence": None,
                    "should_escalate": False,
                }
        except Exception as error:
            print(f"[NLP] Sentiment analysis error: {error}")
            return {
                "sentiment": "neutral",
                "confidence": None,
                "should_escalate": False,
            }

    def preprocess_user_input(self, user_input):
        print("\n" + "=" * 50)
        print("INPUT TRANSFORMATION PIPELINE")
        print("=" * 50)
        print(f"Original input: {user_input}\n")

        detected_lang = self.detect_language(user_input)
        redacted_text = self.redact_pii(user_input)
        text_with_entities = self.extract_entities(redacted_text)
        key_phrases = self.extract_key_phrases(redacted_text)
        sentiment_result = self.analyze_sentiment(redacted_text)

        preprocessed = {
            "original": user_input,
            "redacted": redacted_text,
            "tagged": text_with_entities,
            "language": detected_lang,
            "key_phrases": key_phrases,
            "sentiment": sentiment_result["sentiment"],
            "should_escalate": sentiment_result["should_escalate"],
        }

        return preprocessed
