"""Immutable oracle for cinder-north:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-cinder-north', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
