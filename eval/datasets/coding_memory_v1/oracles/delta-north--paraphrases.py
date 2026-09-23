"""Immutable oracle for delta-north:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-delta-north', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
