from openai import OpenAI
import base64
import argparse
import os
import sys

# --- Configuration ---
# WARNING: Hardcoding API keys is generally discouraged.
API_KEY = "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"
MODEL = "gpt-image-1"

def generate_and_print_base64(prompt: str):
    """Generates an image using OpenAI API and prints its base64 string to stdout."""
    try:
        print(f"Initializing OpenAI client...", file=sys.stderr)
        client = OpenAI(api_key=API_KEY)

        print(f"Generating image for prompt: '{prompt[:50]}...' with model {MODEL}", file=sys.stderr)
        result = client.images.generate(
            model=MODEL,
            prompt=prompt,
            n=1
            # Add size or quality here if needed
        )

        if not result.data or not result.data[0] or not hasattr(result.data[0], 'b64_json') or not result.data[0].b64_json:
            print(f"Error: OpenAI response did not contain expected b64_json data. Response structure: {result}", file=sys.stderr)
            sys.exit(1)

        image_base64 = result.data[0].b64_json
        print(f"Image generated successfully (base64).", file=sys.stderr)
        # Print the base64 string to stdout for the calling process
        print(image_base64)

    except Exception as e:
        print(f"Error during image generation: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate an image using OpenAI API and print base64.")
    # Only needs prompt now
    parser.add_argument("--prompt", required=True, help="Text prompt for image generation.")
    # Removed --output argument

    args = parser.parse_args()

    generate_and_print_base64(args.prompt)