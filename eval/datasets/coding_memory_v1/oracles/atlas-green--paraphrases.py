"""Immutable oracle for atlas-green:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-atlas-green', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
