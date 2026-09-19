"""Immutable oracle for cinder-violet:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-cinder-violet', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
