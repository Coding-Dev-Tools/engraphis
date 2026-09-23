"""Immutable oracle for fjord-north:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-fjord-north', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
