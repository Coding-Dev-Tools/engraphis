"""Immutable oracle for ember-green:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-ember-green', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
