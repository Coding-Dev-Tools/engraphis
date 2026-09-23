"""Immutable oracle for cinder-green:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-cinder-green', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
