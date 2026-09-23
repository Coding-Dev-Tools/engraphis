"""Immutable oracle for delta-west:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-delta-west', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
