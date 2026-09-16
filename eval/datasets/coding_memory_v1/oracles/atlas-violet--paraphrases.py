"""Immutable oracle for atlas-violet:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-atlas-violet', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
