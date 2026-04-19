"""Anthropic Claude client for Polymarket evaluation with web search."""

import json
import logging
import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a superforecaster evaluating Polymarket prediction markets. "
    "Use web search to find the most current information relevant to the question. "
    "Consider base rates, recent evidence, time remaining, and the current market price. "
    "The current market price represents crowd wisdom — explain specifically why you agree or disagree. "
    "Respond with ONLY a valid JSON object with keys: probability (float), confidence (low/medium/high), confidence_score (integer 1-5, where 1=very uncertain, 3=moderate, 5=very confident), reasoning (string, max 2 sentences). "
    "Do not include any text outside the JSON object."
)

QUICK_SYSTEM_PROMPT = (
    "You are a prediction market analyst. Based only on your training knowledge, "
    "estimate the probability this event occurs. Be concise. "
    "Respond with ONLY a JSON object with keys: probability (float 0-1), "
    "confidence (low/medium/high), reasoning (max 1 sentence)."
)


class AnthropicClient:
    """Client for evaluating Polymarket markets using Claude with web search."""

    def __init__(self):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            logger.warning("ANTHROPIC_API_KEY not set — AnthropicClient will not work")
        self.client = anthropic.Anthropic(api_key=api_key)

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """Extract a JSON object from text that may contain surrounding prose."""
        import re

        # Try raw text first
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try extracting from markdown code fence
        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if fence_match:
            try:
                return json.loads(fence_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # Try finding a JSON object in the text
        brace_match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        return None

    def quick_estimate(
        self,
        question: str,
        current_price: float,
    ) -> dict | None:
        """Cheap first-pass estimate using Haiku — no web search.

        Args:
            question: The market question
            current_price: Current Polymarket YES price (0-1)

        Returns:
            Dict with keys: probability, confidence, reasoning, edge.
            None on failure.
        """
        user_message = (
            f"Market: {question}\n"
            f"Current market price: {current_price}\n"
            f"Estimate probability:"
        )

        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                system=QUICK_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )

            text_content = None
            for block in response.content:
                if block.type == "text":
                    text_content = block.text

            if not text_content:
                logger.error("No text content in quick_estimate response")
                return None

            result = self._extract_json(text_content)
            if not result:
                logger.error(f"Could not extract JSON from quick_estimate: {text_content[:200]}")
                return None

            probability = float(result["probability"])
            confidence = result["confidence"].lower()
            reasoning = result["reasoning"]

            if confidence not in ("low", "medium", "high"):
                confidence = "medium"

            probability = max(0.0, min(1.0, probability))

            return {
                "probability": probability,
                "confidence": confidence,
                "reasoning": reasoning[:200],
                "edge": round(probability - current_price, 4),
            }

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse quick_estimate response as JSON: {e}")
            return None
        except KeyError as e:
            logger.error(f"Missing key in quick_estimate response: {e}")
            return None
        except anthropic.APIError as e:
            logger.error(f"Anthropic API error (quick_estimate): {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error in quick_estimate: {e}")
            return None

    def evaluate_market(
        self,
        question: str,
        current_price: float,
        description: str = "",
    ) -> dict | None:
        """Evaluate a Polymarket market using Claude with web search.

        Args:
            question: The market question (e.g., "Will X happen?")
            current_price: Current Polymarket YES price (0-1)
            description: Optional market description/resolution criteria

        Returns:
            Dict with keys: probability, confidence, reasoning, edge.
            None on failure.
        """
        user_message = (
            f"Market question: {question}\n"
            f"Current Polymarket YES price: ${current_price:.2f}\n"
        )
        if description:
            user_message += f"Resolution criteria: {description}\n"
        user_message += (
            "\nSearch the web for the latest information, then estimate the true probability."
        )

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": user_message}],
            )

            # Collect all text blocks — Claude may interleave text with web search
            all_text = []
            for block in response.content:
                if block.type == "text":
                    all_text.append(block.text)

            if not all_text:
                logger.error("No text content in Claude response")
                return None

            # Find JSON in any text block (check last first, most likely location)
            result = None
            for text in reversed(all_text):
                result = self._extract_json(text)
                if result:
                    break

            if not result:
                logger.error(f"Could not extract JSON from response: {all_text[-1][:200]}")
                return None

            # Validate and normalize
            probability = float(result["probability"])
            confidence = result["confidence"].lower()
            reasoning = result["reasoning"]
            confidence_score = max(1, min(5, int(result.get("confidence_score", 3))))

            if confidence not in ("low", "medium", "high"):
                confidence = "medium"

            probability = max(0.0, min(1.0, probability))

            return {
                "probability": probability,
                "confidence": confidence,
                "confidence_score": confidence_score,
                "reasoning": reasoning[:500],
                "edge": round(probability - current_price, 4),
            }

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response as JSON: {e}")
            return None
        except KeyError as e:
            logger.error(f"Missing key in Claude response: {e}")
            return None
        except anthropic.APIError as e:
            logger.error(f"Anthropic API error: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error evaluating market: {e}")
            return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    client = AnthropicClient()
    result = client.evaluate_market(
        question="Will the Federal Reserve cut interest rates before June 2026?",
        current_price=0.45,
        description="Resolves YES if Fed cuts rates at any FOMC meeting before June 30 2026",
    )
    print(result)
